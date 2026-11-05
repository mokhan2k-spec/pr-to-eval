"""Audit an agent transcript for boundary escapes and contamination.

Reads a (redacted) transcript: Claude `stream-json` or Codex `exec --json`. Extracts
EVERY tool call and flags:
  path        a path that resolves outside the workspace (absolute, `..` escape, ~, $HOME)
  canary      the task canary string in the transcript or the patch
  string      a forbidden string in a tool call, the agent's own text, or the patch
              (github.com, pr-to-eval, the upstream slug, /pull/, hidden_tests, ...)
Workspace paths in a stored transcript are the token <WS>; the audit therefore gives
the same answer on the raw and the redacted text.

    python -m harness.audit <transcript.jsonl> [--patch final.patch] [--task-dir tasks/x]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

FORBIDDEN = ["github.com", "pr-to-eval", "/pull/", "hidden_tests", "reference.patch",
             "solutions.json", "expected_tests"]

TEXT_FIELDS = {"content", "new_string", "old_string", "text", "prompt", "description"}

# Path-like tokens inside shell commands.
_WIN = r"(?<![A-Za-z0-9])[A-Za-z]:[\\/][^\s\"'`<>|;,)]*"
_UNC = r"\\\\[^\s\"'`<>|;,)]+"
_UNIX = (r"(?<![\w.:/\\~$-])(?:/[^\s\"'`<>|;,)=/]+/[^\s\"'`<>|;,)=]*"
         r"|/(?:tmp|etc|home|root|var|usr|mnt|opt|proc|Users)(?![\w/.-]))")
_HOME = r"(?:(?<![\w])~[\\/][^\s\"'`<>|;,)]*|\$HOME[^\s\"'`<>|;,)]*|\$env:[A-Za-z_]+[^\s\"'`<>|;,)]*|%[A-Za-z_]+%[^\s\"'`<>|;,)]*)"
_TOKEN = re.compile("|".join((_WIN, _UNC, _UNIX, _HOME)))
_DOTS = re.compile(r"(?<![\w.])(?:\.\.[\\/])+[^\s\"'`<>|;,)]*|(?<![\w.])\.\.(?![\w.])")
_MARKER = re.compile(r"<(?:WS|HOME|REPO|USER)>[^\s\"'`<>|;,)]*")

# Outside the workspace but expected: interpreters, Windows itself, the npm/node tree,
# the repo's node_modules (the junction target), NUL.
_ALLOWED_OUTSIDE = [
    re.compile(p) for p in (
        r"^[a-z]:/windows(/|$)", r"^/[a-z]/windows(/|$)",
        r"/windowspowershell/", r"/powershell/",
        r"(^|/)(powershell|pwsh|cmd|bash|sh|python|python3|node|npx|git)(\.exe)?$",
        r"/appdata/local/programs/python/", r"/python3\d*/", r"/program files(?: \(x86\))?/(nodejs|python|git)(/|$)",
        r"/appdata/roaming/npm(/|$)", r"/appdata/local/microsoft/windowsapps(/|$)",
        r"^<repo>/node_modules(/|$)",
        r"^/dev/(null|stdin|stdout|stderr)$", r"^nul$", r"^/usr/bin/", r"^/bin/",
    )
]


def _norm(p: str) -> str:
    p = p.replace("\\\\", "\\").replace("\\", "/")
    p = re.sub(r"^//\?/", "", p)
    m = re.match(r"^/([A-Za-z])/(.*)$", p)
    if m:
        p = f"{m.group(1)}:/{m.group(2)}"
    return p.lower().rstrip("/") or "/"


def path_escapes(raw: str, ws: str | None = None) -> bool:
    """True if raw (a path as an agent wrote it, workspace-relative or absolute) points
    outside the workspace and is not an allowed system location."""
    raw = raw.strip().strip("\"'`")
    if not raw:
        return False
    p = _norm(raw)
    if p.startswith("<ws>"):
        rest = p[len("<ws>"):].lstrip("/")
        return _climbs_out(rest)
    if ws is not None:
        w = _norm(ws)
        if p == w or p.startswith(w + "/"):
            return False
    if p.startswith(("<home>", "<repo>", "<user>", "~", "$home", "$env:", "%")):
        return not any(rx.search(p) for rx in _ALLOWED_OUTSIDE)
    if re.match(r"^[a-z]:/", p) or p.startswith("//") or p.startswith("/"):
        return not any(rx.search(p) for rx in _ALLOWED_OUTSIDE)
    # relative: resolves against the workspace root
    return _climbs_out(p)


def _climbs_out(rel: str) -> bool:
    """True if a workspace-relative path steps above the workspace root."""
    depth = 0
    for seg in rel.split("/"):
        if seg == "..":
            depth -= 1
            if depth < 0:
                return True
        elif seg not in ("", "."):
            depth += 1
    return False


def _command_paths(cmd: str) -> list[str]:
    found = [m.group(0) for m in _MARKER.finditer(cmd)]
    cmd2 = _MARKER.sub(" ", cmd)
    found += [m.group(0) for m in _TOKEN.finditer(cmd2)]
    found += [m.group(0) for m in _DOTS.finditer(cmd2)]
    return found


def _pathlike(value: str) -> bool:
    return bool(re.match(r"^(?:[A-Za-z]:[\\/]|[\\/]|~|<(?:WS|HOME|REPO|USER)>|\.\.)", value)) \
        or "../" in value or "..\\" in value


def extract_tool_calls(transcript: str) -> list[dict]:
    """Normalised view of a transcript: [{line, kind, tool, fields: {name: str}}, ...].
    kind is "tool" (a tool call input) or "text" (the agent's own words).
    Handles Claude stream-json and Codex --json; unparseable lines are skipped."""
    calls: list[dict] = []
    seen: set = set()
    for n, line in enumerate(transcript.splitlines(), 1):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(ev, dict):
            continue
        item = ev.get("item")
        if isinstance(item, dict):  # Codex
            t = item.get("type")
            key = (t, item.get("id"))
            if t == "command_execution" and isinstance(item.get("command"), str):
                if key not in seen:
                    seen.add(key)
                    calls.append({"line": n, "kind": "tool", "tool": "command_execution",
                                  "fields": {"command": item["command"]}})
            elif t == "file_change":
                if key not in seen:
                    seen.add(key)
                    paths = {f"path{i}": c.get("path", "") for i, c in
                             enumerate(item.get("changes") or []) if isinstance(c, dict)}
                    calls.append({"line": n, "kind": "tool", "tool": "file_change", "fields": paths})
            elif t in ("agent_message", "reasoning") and isinstance(item.get("text"), str):
                if key not in seen:
                    seen.add(key)
                    calls.append({"line": n, "kind": "text", "tool": t, "fields": {"text": item["text"]}})
            elif t in ("mcp_tool_call", "web_search", "collab_tool_call"):
                if key not in seen:
                    seen.add(key)
                    calls.append({"line": n, "kind": "tool", "tool": t,
                                  "fields": {"input": json.dumps(item, ensure_ascii=False)}})
            continue
        msg = ev.get("message")
        if ev.get("type") == "assistant" and isinstance(msg, dict) and isinstance(msg.get("content"), list):
            for block in msg["content"]:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use":
                    inp = block.get("input") or {}
                    fields = {k: v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
                              for k, v in inp.items()} if isinstance(inp, dict) else {"input": str(inp)}
                    calls.append({"line": n, "kind": "tool", "tool": block.get("name", "?"),
                                  "fields": fields})
                elif block.get("type") == "text" and isinstance(block.get("text"), str):
                    calls.append({"line": n, "kind": "text", "tool": "text",
                                  "fields": {"text": block["text"]}})
    return calls


def _hit(kind: str, where: str, text: str) -> dict:
    text = text.replace("\n", " ")
    return {"kind": kind, "where": where, "text": text[:200]}


def audit_transcript(transcript: str, patch: str = "", canary: str | None = None,
                     slug: str | None = None, ws: str | None = None,
                     leak_ids: list[str] | None = None) -> dict:
    """Returns {"clean", "hits": [{kind, where, text}], "tool_calls", "review_required": [...],
    "auditor_digest"}. A hidden-identifier hit is contamination; a host-access pattern only
    sets review_required (cleared at the confirm step)."""
    hits: list[dict] = []
    seen: set = set()

    def add(kind, where, text):
        key = (kind, where, text)
        if key not in seen:
            seen.add(key)
            hits.append(_hit(kind, where, text))

    forbidden = list(FORBIDDEN) + ([slug] if slug else [])
    calls = extract_tool_calls(transcript)
    for c in calls:
        base = f"line {c['line']} {c['tool']}"
        for name, value in c["fields"].items():
            if not isinstance(value, str) or not value:
                continue
            where = f"{base}.{name}"
            if c["kind"] == "tool":
                if name in ("command", "input", "script") or c["tool"] in ("Bash", "PowerShell"):
                    cands = _command_paths(value)
                elif _pathlike(value) or name in ("file_path", "path", "notebook_path") \
                        or name.startswith("path"):
                    cands = [value]
                else:
                    cands = []
                for cand in cands:
                    if path_escapes(cand, ws):
                        add("path", where, cand)
            low = value.lower()
            for s in forbidden:
                if s.lower() in low:
                    add("string", where, f"{s!r} in {value}"[:200])
    if canary:
        if canary in transcript:
            idx = transcript.index(canary)
            add("canary", "transcript", f"canary found at char {idx}")
        if canary in patch:
            add("canary", "patch", "canary found in patch")
    for ln in patch.splitlines():
        if ln.startswith("+") and not ln.startswith("+++"):
            low = ln.lower()
            for s in forbidden:
                if s.lower() in low:
                    add("string", "patch", f"{s!r} in {ln}"[:200])
    for ident in leak_ids or []:
        if ident in transcript_text_fields(calls) or ident in patch:
            add("hidden-identifier", "transcript/patch", f"hidden-test identifier {ident!r}")
    review = host_access_scan(calls, patch)
    return {"clean": not hits, "hits": hits, "tool_calls": len(calls), "review_required": review,
            "auditor_digest": auditor_digest(leak_ids)}


def transcript_text_fields(calls: list[dict]) -> str:
    return chr(10).join(v for c in calls for v in c["fields"].values() if isinstance(v, str))


# --- hidden-material leak check (R11.2) ----------------------------------------------

SOURCE_SUFFIXES = (".py", ".ts", ".tsx", ".mts", ".cts", ".js", ".mjs", ".cjs", ".jsx")
_PY_NAMES = re.compile(r"\b(?:def|class)\s+((?:test|Test)\w+)")
_JS_TITLES = re.compile(r"""\b(?:it|test|describe)(?:\.\w+)*\(\s*(['"`])(.+?)\1""")
_STR_LITERALS = (re.compile(r'"([^"\n\\]{12,})"'), re.compile(r"'([^'\n\\]{12,})'"))


def leak_identifiers(task) -> list[str]:
    """Distinctive identifiers of the HIDDEN tests: test names and string literals that do
    not already appear in TASK.md or the pre-fix upstream tree. If an agent's transcript
    or patch contains one, it saw (or guessed) hidden material."""
    found: set[str] = set()
    for p in sorted(Path(task.hidden_dir).rglob("*")):
        if not p.is_file() or p.suffix not in SOURCE_SUFFIXES:
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        for m in _PY_NAMES.finditer(text):
            found.add(m.group(1))
        for m in _JS_TITLES.finditer(text):
            found.add(m.group(2))
        for rx in _STR_LITERALS:
            for m in rx.finditer(text):
                lit = m.group(1)
                if lit == lit.strip() and re.fullmatch(r"[\w][\w .:/?=&@#%+,'-]*\w", lit) \
                        and not re.fullmatch(r"[\w.@-]+/[\w./-]+", lit) \
                        and re.search(r"[A-Za-z]", lit) and not lit.startswith(("./", "../", "node:", "@")):
                    found.add(lit)
    known = [Path(task.path, "TASK.md").read_text(encoding="utf-8", errors="replace")]
    for p in sorted(Path(task.upstream_dir).rglob("*")):
        if p.is_file() and p.suffix not in (".pyc",):
            try:
                known.append(p.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                pass
    blob = "\n".join(known)
    return sorted(i for i in found if len(i) >= 8 and i not in blob)


def auditor_digest(leak_ids: list[str] | None = None) -> str:
    """Binds an audit to the auditor: this file (code + policy constants) and the task's
    leak-identifier list."""
    import hashlib
    h = hashlib.sha256(Path(__file__).read_bytes())
    h.update(b"\0" + json.dumps(sorted(leak_ids or [])).encode("utf-8"))
    return h.hexdigest()


# --- static host-access scan (R11.3) --------------------------------------------------

_ABS_LIT = r"""["'](?:[A-Za-z]:[\\/]|/(?:etc|home|Users|tmp|var|usr|root|mnt|c)/|~[\\/])"""
_UP_LIT = r"""["'][^"'\n]*\.\.[\\/]|["']\.\.["']"""
PY_PATTERNS = [
    ("home-dir", re.compile(r"Path\.home\(|expanduser")),
    ("os.walk", re.compile(r"os\.walk\(\s*(?![\"']\.[\"']|os\.curdir)")),
    ("absolute-path", re.compile(_ABS_LIT)),
    ("parent-path", re.compile(_UP_LIT)),
    ("socket", re.compile(r"\bimport socket\b|\bsocket\.(?:socket|create_connection|getaddrinfo|gethostbyname)")),
    ("urllib", re.compile(r"\burllib\b")),
    ("requests", re.compile(r"\bimport requests\b|\brequests\.(?:get|post|put|request|Session)")),
    ("http.client", re.compile(r"http\.client|httplib")),
    ("subprocess", re.compile(r"\bsubprocess\b|\bos\.(?:system|popen|exec\w*|spawn\w*)\(")),
]
JS_PATTERNS = [
    ("os.homedir", re.compile(r"os\.homedir|process\.env\.(?:HOME|USERPROFILE)")),
    ("fs-outside", re.compile(r"\bfs\.(?:promises\.)?\w*(?:read|stat|access|open|opendir|glob)\w*\(.*(?:" + _ABS_LIT + "|" + _UP_LIT + ")")),
    ("absolute-path", re.compile(_ABS_LIT)),
    ("parent-path", re.compile(_UP_LIT)),
    ("child_process", re.compile(r"child_process|\bexecSync\(|\bspawnSync?\(|\bexecFile\(")),
    ("fetch", re.compile(r"\bfetch\(|XMLHttpRequest|\bWebSocket\b")),
    ("http-request", re.compile(r"\bhttps?\.(?:request|get)\(|from\s+[\"'](?:node:)?https?[\"']|require\([\"'](?:node:)?https?[\"']\)")),
    ("net", re.compile(r"require\([\"'](?:node:)?(?:net|dgram|tls)[\"']\)|from\s+[\"'](?:node:)?(?:net|dgram|tls)[\"']|\bnet\.(?:connect|createConnection|Socket)")),
]
CMD_PATTERNS = [("network-tool", re.compile(r"\b(?:curl|wget|Invoke-WebRequest|Invoke-RestMethod|iwr)\b"))]


def _patterns_for(name: str) -> list:
    low = name.lower()
    if low.endswith(".py"):
        return PY_PATTERNS
    if low.endswith(SOURCE_SUFFIXES):
        return JS_PATTERNS
    return PY_PATTERNS + JS_PATTERNS + CMD_PATTERNS   # commands and unknown file types


def scan_text(label: str, text: str, patterns: list) -> list[dict]:
    out = []
    for n, line in enumerate(text.splitlines(), 1):
        for name, rx in patterns:
            if rx.search(line):
                out.append({"reason": "host-access-pattern", "pattern": name,
                            "where": f"{label}:{n}", "text": line.strip()[:200]})
    return out


def patch_added_files(patch: str) -> dict[str, str]:
    """{path: added text} from a unified diff (added lines only)."""
    files: dict[str, list[str]] = {}
    cur = None
    for line in patch.splitlines():
        if line.startswith("+++ "):
            tgt = line[4:].strip()
            cur = tgt[2:] if tgt.startswith("b/") else (None if tgt == "/dev/null" else tgt)
            if cur is not None:
                files.setdefault(cur, [])
        elif line.startswith("+") and cur is not None:
            files[cur].append(line[1:])
    return {k: "\n".join(v) for k, v in files.items()}


def host_access_scan(calls: list[dict], patch: str) -> list[dict]:
    """Every file the agent created or changed (patch) plus every file body it wrote and every
    command it ran (transcript). A hit needs review; it is not contamination by itself."""
    seen: set = set()
    out: list[dict] = []

    def add(hits):
        for h in hits:
            key = (h["pattern"], h["where"], h["text"])
            if key not in seen:
                seen.add(key)
                out.append(h)

    for path, text in patch_added_files(patch).items():
        add(scan_text(f"patch {path}", text, _patterns_for(path)))
    for c in calls:
        if c["kind"] != "tool":
            continue
        base = f"line {c['line']} {c['tool']}"
        f = c["fields"]
        for name in ("content", "new_string"):
            if f.get(name):
                add(scan_text(f"{base} {f.get('file_path', '?')}", f[name], _patterns_for(f.get("file_path", ""))))
        for name in ("command", "input", "script"):
            if f.get(name):
                add(scan_text(f"{base}.command", f[name], PY_PATTERNS + JS_PATTERNS + CMD_PATTERNS))
    return out


def main(argv=None) -> int:
    from .tasks import utf8_stdout
    utf8_stdout()
    ap = argparse.ArgumentParser(prog="harness.audit")
    ap.add_argument("transcript")
    ap.add_argument("--patch")
    ap.add_argument("--task-dir")
    ap.add_argument("--slug")
    args = ap.parse_args(argv)
    canary = slug = None
    if args.task_dir:
        td = Path(args.task_dir)
        if (td / "canary.txt").is_file():
            canary = (td / "canary.txt").read_text(encoding="utf-8").strip()
        try:
            slug = json.loads((td / "task.json").read_text(encoding="utf-8"))["upstream"]["repo"]
        except (OSError, KeyError, json.JSONDecodeError):
            pass
    patch = Path(args.patch).read_text(encoding="utf-8", errors="replace") if args.patch else ""
    res = audit_transcript(Path(args.transcript).read_text(encoding="utf-8", errors="replace"),
                           patch=patch, canary=canary, slug=args.slug or slug)
    print(json.dumps(res, indent=2))
    return 0 if res["clean"] else 1


if __name__ == "__main__":
    sys.exit(main())
