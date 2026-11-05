"""Run Claude Code / Codex on each task (5 trials), store immutable evidence, grade it.

Manual only: this spends real money and needs the agent CLIs. It must never be
called from a Makefile target that CI uses.

Layout: runs/<task>/<agent>/trial-<n>/attempt-<m>/ (attempt dirs are written once and
never overwritten). Subcommands:
  (none)   run the selected attempts          python -m harness.agent_runs --task T --agent claude --trials 1-5
  confirm  record the reviewer's classification of one attempt
  regrade  recompute grades and audits whose bindings (patch, task, grader/auditor code) went stale
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from .grade import grade_patch, run_process
from .tasks import ROOT, RUNS_DIR, TASKS_DIR, Task, TaskError, load_tasks, utf8_stdout
from .workspace import remove_tree

PROMPT = ("Read TASK.md in this directory and complete the task. Work only inside this "
          "directory. Run the existing tests if useful. When done, stop.")
CLAUDE_MODEL = "sonnet"
CODEX_MODEL = "gpt-6.1-sol"
CODEX_EFFORT = "medium"
AGENTS = ("claude", "codex")
TRIALS = 5
MAX_ATTEMPTS = 2
DEFAULT_TIMEOUT = 1200
GITIGNORE = ["__pycache__/", ".pytest_cache/", "node_modules/", "node_modules", "*.pyc"]
CODEX_LEAKS = ["~/.codex/AGENTS.md global rules still load (helper-routing rules; "
               "cannot be disabled without moving credentials)"]
CODEX_MODEL_NOTE = ("codex exec --json does not state the model it served; "
                    "observed_model is unknown, only the requested model is recorded")
BOUNDARY_DIR = "_boundary"

# Claude file tools are path-scoped. "relative" = Read(./**); "absolute" = Read(//c/ws/**).
READ_SCOPE = "relative"
CLAUDE_BASH_ALLOW = ["Bash(python -m pytest:*)", "Bash(npx vitest:*)", "Bash(npx tsc:*)",
                     "Bash(git diff:*)", "Bash(git status:*)", "Bash(git log:*)"]
ENV_ALLOW = ["PATH", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "PATHEXT", "USERPROFILE",
             "HOMEDRIVE", "HOMEPATH", "HOME", "APPDATA", "LOCALAPPDATA", "TEMP", "TMP",
             "PROGRAMDATA", "PROGRAMFILES", "PROGRAMFILES(X86)", "NUMBER_OF_PROCESSORS",
             "PROCESSOR_ARCHITECTURE", "OS", "USERNAME", "COMPUTERNAME"]

ATTEMPT_RE = re.compile(r"^attempt-(\d+)$")
TRIAL_RE = re.compile(r"^trial-(\d+)$")


# --- small helpers ---------------------------------------------------------

def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str | None:
    try:
        return sha256_bytes(Path(path).read_bytes())
    except OSError:
        return None


def read_json(path: Path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def write_json(path: Path, data) -> None:
    Path(path).write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                          encoding="utf-8", newline="\n")


def runs_root() -> Path:
    """Temp root for per-attempt workspaces (outside the repo, outside OneDrive)."""
    return Path(tempfile.gettempdir()) / "pr2e-runs"


def task_hash(task: Task) -> str:
    """sha256 over sorted (relpath, bytes) of everything that decides a grade:
    task.json, TASK.md, checks.sh, grader_prompt.md, expected_tests.json, hidden/**, upstream/**."""
    base = task.path
    files: dict[str, Path] = {}
    for name in ("task.json", "TASK.md", "checks.sh", "grader_prompt.md", "expected_tests.json"):
        if (base / name).is_file():
            files[name] = base / name
    for sub in ("hidden", "upstream"):
        root = base / sub
        if not root.is_dir():
            continue
        for p in root.rglob("*"):
            parts = p.relative_to(base).parts
            if not p.is_file() or "__pycache__" in parts or ".pytest_cache" in parts \
                    or p.suffix == ".pyc":
                continue
            files["/".join(parts)] = p
    h = hashlib.sha256()
    for rel in sorted(files):
        data = files[rel].read_bytes()
        h.update(rel.encode("utf-8") + b"\0" + str(len(data)).encode() + b"\0" + data)
    return h.hexdigest()

BOUNDARY_STATEMENT = (
    "Direct outside reads/network by tools: denied for Claude (path-scoped tools, Bash allowlist). "
    "Codex: network denied, reads detected. "
    "Code the agent runs: detected (leak check plus static scan), NOT prevented. "
    "A documented limitation.")

HARNESS_DIR = Path(__file__).parent
_TOOLCHAIN: dict | None = None


def toolchain_versions() -> dict:
    """python, node, pytest, vitest versions (cached for the process)."""
    global _TOOLCHAIN
    if _TOOLCHAIN is not None:
        return _TOOLCHAIN
    from importlib import metadata
    v = {"python": sys.version.split()[0]}
    try:
        v["pytest"] = metadata.version("pytest")
    except metadata.PackageNotFoundError:
        v["pytest"] = None
    try:
        res = subprocess.run(["node", "--version"], capture_output=True, text=True, timeout=30)
        v["node"] = res.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        v["node"] = None
    pkg = read_json(ROOT / "node_modules" / "vitest" / "package.json")
    v["vitest"] = pkg.get("version") if isinstance(pkg, dict) else None
    _TOOLCHAIN = v
    return v


def grader_digest(task: Task) -> str:
    """sha256 over the grading CODE and environment: grade.py, workspace.py, the task's
    checks.sh, package-lock.json, requirements.txt, and the toolchain versions."""
    h = hashlib.sha256()
    here = HARNESS_DIR
    parts = [here / "grade.py", here / "workspace.py", task.checks_path,
             ROOT / "package-lock.json", ROOT / "requirements.txt"]
    for p in parts:
        data = p.read_bytes() if p.is_file() else b"<missing>"
        h.update(p.name.encode() + b"\0" + str(len(data)).encode() + b"\0" + data)
    h.update(json.dumps(toolchain_versions(), sort_keys=True).encode())
    return h.hexdigest()


def task_auditor_digest(task: Task) -> tuple[str, list[str]]:
    from . import audit as audit_mod
    ids = audit_mod.leak_identifiers(task)
    return audit_mod.auditor_digest(ids), ids


# --- privacy: redaction ----------------------------------------------------

def _win_names(p: str) -> set[str]:
    """Short (8.3) and long spellings of an existing Windows path."""
    out: set[str] = set()
    if os.name != "nt" or not os.path.exists(p):
        return out
    try:
        import ctypes
        for fn in (ctypes.windll.kernel32.GetShortPathNameW,
                   ctypes.windll.kernel32.GetLongPathNameW):
            buf = ctypes.create_unicode_buffer(1024)
            if fn(p, buf, 1024):
                out.add(buf.value)
    except (OSError, AttributeError):
        pass
    return out


def path_forms(path: Path | str) -> set[str]:
    """Every spelling a transcript may use for path: backslash, forward, msys (/c/...),
    each also JSON-escaped (doubled backslashes) and double-escaped."""
    p = str(path)
    cands = {p}
    try:
        cands.add(os.path.realpath(p))
    except OSError:
        pass
    for c in list(cands):
        cands |= _win_names(c)
    forms: set[str] = set()
    for c in cands:
        if not c:
            continue
        c = c.removeprefix("\\\\?\\")
        back = c.replace("/", "\\")
        fwd = c.replace("\\", "/")
        forms |= {back, fwd, back.replace("\\", "\\\\"), back.replace("\\", "\\\\\\\\")}
        m = re.match(r"^([A-Za-z]):/(.*)$", fwd)
        if m:
            forms.add(f"/{m.group(1).lower()}/{m.group(2)}")
    return {f.rstrip("\\/") for f in forms if f.rstrip("\\/")}


class Redactor:
    """Replace machine-specific strings: attempt ws -> <WS>, repo -> <REPO>, home -> <HOME>,
    login name -> <USER>. Case-insensitive on Windows."""

    def __init__(self, ws: Path | None = None, repo: Path | None = None,
                 home: Path | None = None, user: str | None = None):
        flags = re.IGNORECASE if os.name == "nt" else 0
        self.steps: list[tuple[re.Pattern, str]] = []
        for base, tag in ((ws, "<WS>"), (repo if repo is not None else ROOT, "<REPO>"),
                          (home if home is not None else Path.home(), "<HOME>")):
            if base is None:
                continue
            forms = sorted(path_forms(base), key=len, reverse=True)
            self.steps.append((re.compile("|".join(re.escape(f) for f in forms), flags), tag))
        names = {n for n in (user, _login(), os.environ.get("USERNAME")) if n}
        if names:
            alt = "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True))
            self.steps.append((re.compile(rf"(?<![A-Za-z0-9])(?:{alt})(?![A-Za-z0-9])", re.I), "<USER>"))
        # grading workspaces live under <REPO>/.work/<uuid>
        self.work = re.compile(r"<REPO>[\\/]+\.work[\\/]+[0-9a-f]{32}")

    def __call__(self, text: str) -> str:
        for rx, tag in self.steps:
            text = rx.sub(tag, text)
        return self.work.sub("<WS>", text)

    def obj(self, value):
        if isinstance(value, str):
            return self(value)
        if isinstance(value, list):
            return [self.obj(v) for v in value]
        if isinstance(value, dict):
            return {k: self.obj(v) for k, v in value.items()}
        return value


def _login() -> str | None:
    try:
        return os.getlogin()
    except OSError:
        return None


# --- agent command lines ---------------------------------------------------

def resolve_exe(agent: str) -> list[str]:
    """argv[0..] that launches the CLI. On Windows the npm .cmd shims go through cmd.exe,
    which mangles quotes and parentheses in arguments, so launch the real binary."""
    found = shutil.which(agent)
    if not found:
        return [agent]
    p = Path(found)
    if os.name == "nt" and p.suffix.lower() in (".cmd", ".bat"):
        base = p.parent / "node_modules"
        native = base / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe"
        js = base / "@openai" / "codex" / "bin" / "codex.js"
        if agent == "claude" and native.exists():
            return [str(native)]
        if agent == "codex" and js.exists():
            node = shutil.which("node") or "node"
            return [node, str(js)]
    return [found]


def claude_allowed_tools(ws: Path | None = None, scope: str | None = None) -> list[str]:
    scope = scope or READ_SCOPE
    if scope == "absolute" and ws is not None:
        fwd = Path(ws).resolve().as_posix()
        m = re.match(r"^([A-Za-z]):/(.*)$", fwd)
        root = f"//{m.group(1).lower()}/{m.group(2)}" if m else fwd
        file_tools = [f"Read({root}/**)", f"Edit({root}/**)", f"Write({root}/**)"]
    else:
        file_tools = ["Read(./**)", "Edit(./**)", "Write(./**)"]
    return file_tools + ["Glob", "Grep"] + CLAUDE_BASH_ALLOW


def build_command(agent: str, ws: Path, exe=None, scope: str | None = None,
                  prompt: str | None = None) -> list[str]:
    prompt = prompt or PROMPT
    exe = [exe] if isinstance(exe, str) else list(exe) if exe else [agent]
    if agent == "claude":
        return exe + ["-p", prompt, "--model", CLAUDE_MODEL,
                      "--setting-sources", "project", "--strict-mcp-config",
                      "--no-session-persistence", "--permission-mode", "dontAsk",
                      "--allowedTools", *claude_allowed_tools(ws, scope),
                      "--disallowedTools", "WebFetch", "WebSearch", "Agent",
                      "--output-format", "stream-json", "--verbose"]
    cmd = exe + ["exec", "--ignore-user-config", "--ephemeral", "--skip-git-repo-check",
                 "-s", "workspace-write"]
    if sys.platform == "win32":
        cmd += ["-c", 'windows.sandbox="elevated"']
    cmd += ["-c", "sandbox_workspace_write.network_access=false",
            "-c", "project_doc_max_bytes=0", "-m", CODEX_MODEL,
            "-c", f'model_reasoning_effort="{CODEX_EFFORT}"', "--json", "-C", str(ws), prompt]
    return cmd


def agent_env(agent: str) -> dict:
    """Allowlisted environment: no inherited tokens or API keys."""
    env = {k: v for k, v in os.environ.items() if k.upper() in ENV_ALLOW}
    if agent == "claude":
        env["CLAUDE_CODE_DISABLE_CLAUDE_MDS"] = "1"
    return env


def clean_env(extra: dict | None = None) -> dict:
    env = {k: v for k, v in os.environ.items() if k.upper() in ENV_ALLOW}
    env["CLAUDE_CODE_DISABLE_CLAUDE_MDS"] = "1"
    env.update(extra or {})
    return env


def cli_version(agent: str, exe=None) -> str:
    cmd = (list(exe) if exe else resolve_exe(agent)) + ["--version"]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                             errors="replace", timeout=60, stdin=subprocess.DEVNULL,
                             env=agent_env(agent))
        return (res.stdout or res.stderr).strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return f"error: {type(e).__name__}"


# --- workspace and patch capture ------------------------------------------

def git(ws: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(ws), *args], capture_output=True,
                          check=check, stdin=subprocess.DEVNULL)


def copy_node_modules(ws: Path) -> None:
    """Each workspace gets its OWN copy of the toolchain: a shared writable junction would let
    one trial alter what the next one (or the repo) runs."""
    src = ROOT / "node_modules"
    if not src.is_dir():
        raise RuntimeError("node_modules is missing: run `npm ci` in the repo first")
    shutil.copytree(src, ws / "node_modules")


def new_workspace_path(task: Task, agent: str, trial: int, attempt: int) -> Path:
    return runs_root() / task.id / agent / f"trial-{trial}" / f"attempt-{attempt}-{secrets.token_hex(4)}"


def prepare_workspace(task: Task, ws: Path) -> str:
    """Workspace = copy of upstream/ + TASK.md + .gitignore, one baseline commit.
    Returns the baseline commit SHA."""
    ws = Path(ws)
    if ws.exists():
        remove_tree(ws)
    ws.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(task.upstream_dir, ws)
    (ws / "TASK.md").write_text(task.task_md(), encoding="utf-8", newline="\n")
    gi = ws / ".gitignore"
    existing = gi.read_text(encoding="utf-8") if gi.exists() else ""
    gi.write_text(existing + ("" if not existing or existing.endswith("\n") else "\n")
                  + "\n".join(GITIGNORE) + "\n", encoding="utf-8", newline="\n")
    git(ws, "init", "-q")
    git(ws, "config", "user.name", "pr-to-eval")
    git(ws, "config", "user.email", "pr-to-eval@example.invalid")
    git(ws, "config", "core.autocrlf", "false")
    git(ws, "add", "-A")
    git(ws, "commit", "-q", "-m", "baseline")
    if task.language == "typescript":
        copy_node_modules(ws)
    return git(ws, "rev-parse", "HEAD").stdout.decode().strip()


def capture_patch(ws: Path, baseline_sha: str) -> bytes:
    """Everything the agent changed since the baseline, committed or not."""
    git(ws, "add", "-A")
    res = git(ws, "diff", "--binary", "--cached", baseline_sha, "--", ".", ":(exclude)node_modules")
    return res.stdout


# --- classification --------------------------------------------------------

def classify(grade: dict, patch: bytes, meta: dict, audit: dict | None = None) -> dict:
    """Auto-classifier PROPOSAL. A clean PASS is confirmed automatically (deterministic
    evidence); every other outcome waits for `confirm` by the main seat."""
    if audit is not None and not audit.get("clean", True):
        hits = "; ".join(f"{h['kind']}: {h['text']}" for h in audit["hits"][:5])
        return {"outcome": "contaminated", "failure_source": None,
                "evidence": f"audit not clean: {hits}", "classified_by": "auto",
                "confirmed": False}
    review = (audit or {}).get("review_required") or []
    if grade["verdict"] == "PASS" and review:
        first = review[0]
        return {"outcome": "pass", "failure_source": None, "classified_by": "auto",
                "confirmed": False,
                "evidence": f"passed, but {len(review)} host-access pattern(s) need review; "
                            f"first: {first['where']}: {first['text']}"}
    if grade["verdict"] == "PASS":
        return {"outcome": "pass", "failure_source": None,
                "evidence": "hidden tests and shell checks all passed, audit clean",
                "classified_by": "auto", "confirmed": True, "confirmed_by": "auto"}
    out = {"classified_by": "auto", "confirmed": False}
    empty = not patch.strip()
    if grade["verdict"] == "ERROR":
        diag = (grade.get("runner") or {}).get("diag_tail", "").strip().splitlines()
        out.update(outcome="error", failure_source="environment",
                   evidence="grader infrastructure error: " + (diag[-1] if diag else "see grade.json"))
        return out
    out["outcome"] = "fail"
    if grade.get("apply_error"):
        out.update(failure_source="environment",
                   evidence=f"final.patch does not apply to a fresh workspace: {grade['apply_error']}")
    elif empty and meta.get("timed_out"):
        out.update(failure_source="environment", evidence="timed out with no patch")
    elif empty and meta.get("exit_code") not in (0, None):
        out.update(failure_source="environment",
                   evidence=f"no patch and CLI exit code {meta.get('exit_code')}; see stderr.txt")
    elif empty:
        out.update(failure_source="agent", evidence="agent exited cleanly but changed nothing")
    else:
        failed = grade["tests"]["failed"] + grade["checks"]["failed"]
        out.update(failure_source="agent",
                   evidence="patch applies but grading failed: " + (", ".join(failed[:10]) or "no tests ran"))
    return out


def bind_classification(cls: dict, patch_sha: str, grade_path: Path, audit_path: Path) -> dict:
    cls = dict(cls)
    cls.update(patch_sha256=patch_sha, grade_sha256=sha256_file(grade_path),
               audit_sha256=sha256_file(audit_path))
    return cls


# --- attempt bookkeeping ---------------------------------------------------

def trial_dir(runs_dir: Path, task_id: str, agent: str, trial: int) -> Path:
    return Path(runs_dir) / task_id / agent / f"trial-{trial}"


def list_attempts(tdir: Path) -> list[tuple[int, Path]]:
    out = []
    if tdir.is_dir():
        for p in tdir.iterdir():
            m = ATTEMPT_RE.match(p.name)
            if m and p.is_dir():
                out.append((int(m.group(1)), p))
    return sorted(out)


def retry_allowed(prev_dir: Path) -> tuple[bool, str]:
    """A new attempt needs the previous attempt's CONFIRMED classification to be
    environment or contaminated."""
    meta = read_json(prev_dir / "meta.json") or {}
    if meta.get("status") != "complete":
        return True, "previous attempt is incomplete"      # it still used up an attempt
    cls = read_json(prev_dir / "classification.json")
    if not cls:
        return False, "previous attempt has no classification.json"
    if not cls.get("confirmed"):
        return False, "previous attempt's classification is not confirmed (run `confirm`)"
    if cls.get("stale"):
        return False, "previous attempt's classification is stale"
    if cls.get("outcome") == "contaminated" or cls.get("failure_source") == "environment":
        return True, ""
    return False, "previous attempt is final (not environment/contaminated)"


def plan_attempt(runs_dir: Path, task_id: str, agent: str, trial: int) -> tuple[int | None, str]:
    """Next attempt number for a trial, or (None, reason)."""
    attempts = list_attempts(trial_dir(runs_dir, task_id, agent, trial))
    if not attempts:
        return 1, ""
    last_n, last_dir = attempts[-1]
    ok, why = retry_allowed(last_dir)
    if not ok:
        return None, why
    if last_n >= MAX_ATTEMPTS:
        return None, f"max {MAX_ATTEMPTS} attempts per trial reached"
    return last_n + 1, ""


# --- transcript facts ------------------------------------------------------

def observed_model(agent: str, transcript: str) -> str | None:
    for line in transcript.splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(ev, dict):
            continue
        if agent == "claude" and ev.get("type") == "system" and ev.get("subtype") == "init":
            return ev.get("model")
        if agent == "codex":
            for src in (ev, ev.get("item") if isinstance(ev.get("item"), dict) else {}):
                if isinstance(src.get("model"), str):
                    return src["model"]
    return None


def boundary_summary(runs_dir: Path, agent: str):
    probe = read_json(Path(runs_dir) / BOUNDARY_DIR / "probe.json")
    if not probe:
        return None
    return (probe.get("agents") or {}).get(agent)


# --- one attempt -----------------------------------------------------------

def _finalise_incomplete(adir: Path, meta: dict, exc: BaseException, ws: Path | None,
                         raw: Path | None, baseline: str | None) -> None:
    """Best-effort evidence for an attempt that died after the model may have been launched.
    The directory is never deleted: it stays as status=incomplete and counts as an attempt."""
    import traceback
    red = Redactor(ws=ws) if ws else Redactor()
    meta = dict(meta)
    meta.update(status="incomplete", ended_utc=now_utc(),
                error="".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
    try:
        if raw is not None and (raw / "transcript.jsonl").is_file():
            (adir / "transcript.jsonl").write_text(
                red((raw / "transcript.jsonl").read_text(encoding="utf-8", errors="replace")),
                encoding="utf-8", newline="\n")
            if (raw / "stderr.txt").is_file():
                (adir / "stderr.txt").write_text(
                    red((raw / "stderr.txt").read_text(encoding="utf-8", errors="replace")),
                    encoding="utf-8", newline="\n")
    except OSError as e:
        meta["salvage_note"] = f"transcript not saved: {e}"
    try:
        if ws is not None and baseline and ws.is_dir() and not (adir / "final.patch").exists():
            (adir / "final.patch").write_bytes(capture_patch(ws, baseline))
    except Exception as e:  # noqa: BLE001 - salvage must never raise
        meta["salvage_note"] = (meta.get("salvage_note", "") + f" patch not saved: {e}").strip()
    write_json(adir / "meta.json", red.obj(meta))


def run_attempt(task: Task, agent: str, trial: int, attempt: int, runs_dir: Path,
                timeout: int = DEFAULT_TIMEOUT, exe=None, keep_ws: bool = False) -> dict:
    """Run one agent attempt end to end into runs/<task>/<agent>/trial-<n>/attempt-<m>/.

    The attempt directory is created exclusively and meta.json (status=started) is written
    BEFORE the model is launched, so a crash can never make a paid attempt vanish. Any
    failure afterwards finalises it as status=incomplete (traceback + whatever transcript /
    patch exists). The directory is never deleted and never written again once complete."""
    from . import audit as audit_mod
    runs_dir = Path(runs_dir)
    adir = trial_dir(runs_dir, task.id, agent, trial) / f"attempt-{attempt}"
    adir.parent.mkdir(parents=True, exist_ok=True)
    try:
        adir.mkdir()
    except FileExistsError:
        raise FileExistsError(f"{adir} already exists; attempts are immutable") from None
    meta = {"task": task.id, "agent": agent, "trial": trial, "attempt": attempt,
            "status": "started", "started_utc": now_utc(), "timeout_s": timeout,
            "requested_model": CLAUDE_MODEL if agent == "claude" else CODEX_MODEL,
            "requested_effort": None if agent == "claude" else CODEX_EFFORT,
            "read_scope": READ_SCOPE if agent == "claude" else None,
            "known_context_leak": CODEX_LEAKS if agent == "codex" else [],
            "boundary": {"statement": BOUNDARY_STATEMENT,
                         "probe": boundary_summary(runs_dir, agent)}}
    write_json(adir / "meta.json", meta)

    ws = raw = baseline = None
    try:
        ws = new_workspace_path(task, agent, trial, attempt)
        baseline = prepare_workspace(task, ws)
        raw = ws.with_name(ws.name + ".raw")  # unredacted logs, outside the agent's workspace
        raw.mkdir(parents=True, exist_ok=True)
        red = Redactor(ws=ws)
        exe_list = exe if exe is not None else resolve_exe(agent)
        cmd = build_command(agent, ws, exe_list)
        started = time.time()
        meta.update(cli_version=cli_version(agent, exe_list if exe is not None else None),
                    baseline_sha=baseline, env_keys=sorted(agent_env(agent)))
        write_json(adir / "meta.json", Redactor(ws=ws).obj(meta))
        with open(raw / "transcript.jsonl", "w", encoding="utf-8", newline="\n") as out, \
                open(raw / "stderr.txt", "w", encoding="utf-8", newline="\n") as err:
            res = run_process(cmd, cwd=ws, env=agent_env(agent), timeout=timeout,
                              stdout=out, stderr=err, stdin=subprocess.DEVNULL)
        raw_transcript = (raw / "transcript.jsonl").read_text(encoding="utf-8", errors="replace")
        raw_stderr = (raw / "stderr.txt").read_text(encoding="utf-8", errors="replace")
        obs = observed_model(agent, raw_transcript)
        meta.update(ended_utc=now_utc(), duration_s=round(time.time() - started, 1),
                    exit_code=res.returncode, timed_out=res.timed_out, observed_model=obs)
        if obs is None and agent == "codex":
            meta["observed_model_note"] = CODEX_MODEL_NOTE
        meta["argv"] = [Path(c).name if i == 0 else c for i, c in enumerate(cmd)] \
            if exe is None else ["<test-exe>"] + cmd[len(exe_list):]

        patch = capture_patch(ws, baseline)
        # redacted evidence goes into the repo; the raw copies stay in the temp dir
        transcript = red(raw_transcript)
        (adir / "transcript.jsonl").write_text(transcript, encoding="utf-8", newline="\n")
        (adir / "stderr.txt").write_text(red(raw_stderr), encoding="utf-8", newline="\n")
        (adir / "final.patch").write_bytes(patch)

        patch_sha = sha256_bytes(patch)
        canary_file = task.path / "canary.txt"
        canary = canary_file.read_text(encoding="utf-8").strip() if canary_file.is_file() else None
        _, leak_ids = task_auditor_digest(task)
        aud = audit_mod.audit_transcript(transcript, patch=patch.decode("utf-8", errors="replace"),
                                         canary=canary, slug=task.upstream.get("repo"),
                                         leak_ids=leak_ids)
        aud.update(transcript_sha256=sha256_bytes(transcript.encode("utf-8")), patch_sha256=patch_sha)
        write_json(adir / "audit.json", aud)

        grade = red.obj(grade_patch(task, adir / "final.patch"))
        grade.update(patch_sha256=patch_sha, task_hash=task_hash(task), grader_digest=grader_digest(task))
        write_json(adir / "grade.json", grade)

        cls = bind_classification(classify(grade, patch, red.obj(meta), aud), patch_sha,
                                  adir / "grade.json", adir / "audit.json")
        write_json(adir / "classification.json", cls)

        meta["status"] = "complete"
        write_json(adir / "meta.json", red.obj(meta))     # last: marks the attempt finished
    except BaseException as e:  # noqa: BLE001 - finalise the paid attempt, then propagate interrupts
        _finalise_incomplete(adir, meta, e, ws, raw, baseline)
        if not isinstance(e, Exception):
            raise
        return {"label": f"{task.id}/{agent}/trial-{trial}/attempt-{attempt}", "verdict": None,
                "classification": None, "dir": adir, "status": "incomplete",
                "error": f"{type(e).__name__}: {e}"}
    if not keep_ws:
        remove_tree(ws)
    return {"label": f"{task.id}/{agent}/trial-{trial}/attempt-{attempt}",
            "verdict": grade["verdict"], "classification": cls, "dir": adir, "status": "complete"}


# --- subcommands -----------------------------------------------------------

def parse_trials(spec: str) -> list[int]:
    out: list[int] = []
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-", 1)
            out += range(int(a), int(b) + 1)
        elif part:
            out.append(int(part))
    bad = [n for n in out if n < 1 or n > TRIALS]
    if bad or not out:
        raise ValueError(f"--trials must be within 1-{TRIALS}, got {spec!r}")
    return sorted(set(out))


def confirm_attempt(attempt_dir: Path, source: str, evidence: str,
                    audit_false_positive: bool = False, review_cleared: str | None = None) -> dict:
    """Record the main seat's classification. `source` is agent / environment / grader /
    contaminated, or `pass` to confirm a PASS verdict. If the audit has review_required
    entries (host-access patterns), `review_cleared` must say why they are acceptable."""
    attempt_dir = Path(attempt_dir)
    if not evidence or not evidence.strip():
        raise ValueError("evidence must be non-blank (quote the lines you relied on)")
    meta = read_json(attempt_dir / "meta.json") or {}
    if meta.get("status") != "complete":
        raise ValueError(f"{attempt_dir}: attempt is {meta.get('status', 'unknown')}, not complete")
    grade = read_json(attempt_dir / "grade.json")
    if not grade:
        raise ValueError(f"{attempt_dir}: no grade.json")
    aud = read_json(attempt_dir / "audit.json") or {}
    review = aud.get("review_required") or []
    if review and not (review_cleared and review_cleared.strip()):
        raise ValueError(f"audit.json has {len(review)} review_required host-access pattern(s) "
                         f"(first: {review[0]['where']}: {review[0]['text']}); "
                         "pass --review-cleared '<why they are acceptable>'")
    verdict = grade["verdict"]
    if source == "contaminated":
        outcome, fsrc = "contaminated", None
    elif source == "pass":
        if verdict != "PASS":
            raise ValueError(f"verdict is {verdict}; --source pass confirms PASS verdicts only")
        outcome, fsrc = "pass", None
    else:
        if verdict == "PASS":
            raise ValueError("verdict is PASS; use --source pass (or contaminated)")
        outcome, fsrc = ("error" if verdict == "ERROR" else "fail"), source
    cls = {"outcome": outcome, "failure_source": fsrc, "evidence": evidence.strip(),
           "classified_by": "main-seat", "confirmed": True, "confirmed_by": "main-seat",
           "confirmed_utc": now_utc()}
    if audit_false_positive:
        cls["audit_override"] = True
    if review:
        cls["review_cleared"] = review_cleared.strip()
    patch_sha = sha256_file(attempt_dir / "final.patch")
    cls = bind_classification(cls, patch_sha, attempt_dir / "grade.json", attempt_dir / "audit.json")
    write_json(attempt_dir / "classification.json", cls)
    return cls


def _mark_stale(attempt_dir: Path, names, reason: str) -> None:
    for name in names:
        data = read_json(attempt_dir / name)
        if data is not None:
            data.update(stale=True, stale_reason=reason)
            write_json(attempt_dir / name, data)


def _append_history(attempt_dir: Path, name: str, old: dict | None) -> None:
    if old is not None:
        with open(attempt_dir / name, "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(dict(old, superseded_utc=now_utc()), ensure_ascii=False) + "\n")


def regrade_attempt(task: Task, attempt_dir: Path, force: bool = False) -> bool:
    """Recompute grade.json if it no longer matches the patch, the task or the grader code /
    toolchain (grader_digest). The old grade is appended to grade-history.jsonl; the
    classification and llm_grade are marked stale and need re-confirmation."""
    attempt_dir = Path(attempt_dir)
    patch = attempt_dir / "final.patch"
    if not patch.is_file():
        return False
    old = read_json(attempt_dir / "grade.json")
    cur_patch, cur_task, cur_grader = sha256_file(patch), task_hash(task), grader_digest(task)
    fresh = old and old.get("patch_sha256") == cur_patch and old.get("task_hash") == cur_task \
        and old.get("grader_digest") == cur_grader
    if fresh and not force:
        return False
    _append_history(attempt_dir, "grade-history.jsonl", old)
    grade = Redactor().obj(grade_patch(task, patch))
    grade.update(patch_sha256=cur_patch, task_hash=cur_task, grader_digest=cur_grader)
    write_json(attempt_dir / "grade.json", grade)
    _mark_stale(attempt_dir, ("classification.json", "llm_grade.json"),
                "grade.json was regraded; re-run confirm / llm_grade")
    return True


def reaudit_attempt(task: Task, attempt_dir: Path, force: bool = False) -> bool:
    """Recompute audit.json from the stored transcript and patch if the transcript, patch or
    auditor (audit.py + policy + the task's leak identifiers) changed. Invalidates the
    classification."""
    from . import audit as audit_mod
    attempt_dir = Path(attempt_dir)
    tpath, ppath = attempt_dir / "transcript.jsonl", attempt_dir / "final.patch"
    if not tpath.is_file() or not ppath.is_file():
        return False
    old = read_json(attempt_dir / "audit.json")
    digest, ids = task_auditor_digest(task)
    t_sha, p_sha = sha256_file(tpath), sha256_file(ppath)
    fresh = old and old.get("auditor_digest") == digest and old.get("transcript_sha256") == t_sha \
        and old.get("patch_sha256") == p_sha
    if fresh and not force:
        return False
    _append_history(attempt_dir, "audit-history.jsonl", old)
    canary_file = task.path / "canary.txt"
    canary = canary_file.read_text(encoding="utf-8").strip() if canary_file.is_file() else None
    aud = audit_mod.audit_transcript(tpath.read_text(encoding="utf-8"),
                                     patch=ppath.read_bytes().decode("utf-8", errors="replace"),
                                     canary=canary, slug=task.upstream.get("repo"), leak_ids=ids)
    aud.update(transcript_sha256=t_sha, patch_sha256=p_sha)
    write_json(attempt_dir / "audit.json", aud)
    _mark_stale(attempt_dir, ("classification.json",), "audit.json was recomputed; re-run confirm")
    return True


def _selected_dirs(runs_dir: Path, task_ids, agents):
    for tdir in sorted(Path(runs_dir).glob("*/*/trial-*/attempt-*")):
        if not ATTEMPT_RE.match(tdir.name) or not tdir.is_dir():
            continue
        task_id, agent = tdir.parts[-4], tdir.parts[-3]
        if task_id.startswith("_") or (task_ids and task_id not in task_ids) \
                or (agents and agent not in agents):
            continue
        yield task_id, tdir


def cmd_regrade(args) -> int:
    tasks = {t.id: t for t in load_tasks(Path(args.tasks_dir))}
    agents = None if args.agent == "all" else (args.agent,)
    n = 0
    for task_id, adir in _selected_dirs(Path(args.runs_dir), {args.task} if args.task else None, agents):
        if task_id not in tasks:
            print(f"{adir}: unknown task", file=sys.stderr)
            continue
        label = f"{task_id}/{adir.parts[-3]}/{adir.parts[-2]}/{adir.name}"
        if reaudit_attempt(tasks[task_id], adir, force=args.all):
            n += 1
            print(f"re-audited {label}")
        if regrade_attempt(tasks[task_id], adir, force=args.all):
            n += 1
            print(f"regraded {label}")
    print(f"{n} regraded / re-audited")
    return 0


def cmd_confirm(args) -> int:
    try:
        cls = confirm_attempt(Path(args.attempt_dir), args.source, args.evidence,
                              args.audit_false_positive, args.review_cleared)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(f"confirmed: outcome={cls['outcome']} failure_source={cls['failure_source']}")
    return 0


def parse_exe(items: list[str] | None) -> dict:
    out = {}
    for item in items or []:
        agent, _, path = item.partition("=")
        if agent not in AGENTS or not path:
            raise ValueError(f"--exe expects claude=PATH or codex=PATH, got {item!r}")
        out[agent] = [path]
    return out


def main(argv=None) -> int:
    utf8_stdout()
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "confirm":
        ap = argparse.ArgumentParser(prog="harness.agent_runs confirm")
        ap.add_argument("attempt_dir")
        ap.add_argument("--source", required=True,
                        choices=("agent", "environment", "grader", "contaminated", "pass"))
        ap.add_argument("--evidence", required=True)
        ap.add_argument("--audit-false-positive", action="store_true",
                        help="the audit hits were reviewed and are not contamination")
        ap.add_argument("--review-cleared", help="why the audit's review_required host-access "
                        "patterns are acceptable (required when there are any)")
        return cmd_confirm(ap.parse_args(argv[1:]))
    if argv and argv[0] == "regrade":
        ap = argparse.ArgumentParser(prog="harness.agent_runs regrade")
        ap.add_argument("--task")
        ap.add_argument("--agent", choices=("claude", "codex", "all"), default="all")
        ap.add_argument("--all", action="store_true", help="regrade even if the hashes match")
        ap.add_argument("--tasks-dir", default=str(TASKS_DIR))
        ap.add_argument("--runs-dir", default=str(RUNS_DIR))
        return cmd_regrade(ap.parse_args(argv[1:]))

    ap = argparse.ArgumentParser(prog="harness.agent_runs")
    ap.add_argument("--task")
    ap.add_argument("--tasks-dir", default=str(TASKS_DIR))
    ap.add_argument("--runs-dir", default=str(RUNS_DIR))
    ap.add_argument("--agent", choices=("claude", "codex", "all"), default="all")
    ap.add_argument("--trials", default=f"1-{TRIALS}", help="e.g. 1-5, 3, 1,4")
    ap.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    ap.add_argument("--jobs", type=int, default=3)
    ap.add_argument("--exe", action="append", help="override an agent binary: claude=PATH")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    try:
        tasks = load_tasks(Path(args.tasks_dir), args.task)
        trials = parse_trials(args.trials)
        exes = parse_exe(args.exe)
    except (TaskError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    runs_dir = Path(args.runs_dir)
    agents = AGENTS if args.agent == "all" else (args.agent,)
    jobs = []
    for t in tasks:
        for a in agents:
            for n in trials:
                m, why = plan_attempt(runs_dir, t.id, a, n)
                if m is None:
                    print(f"{t.id}/{a}/trial-{n}: skipped ({why or 'done'})")
                else:
                    jobs.append((t, a, n, m))
    if args.dry_run:
        for t, a, n, m in jobs:
            ws = runs_root() / t.id / a / f"trial-{n}" / f"attempt-{m}-<rand8>"
            print(f"[dry-run] {t.id}/{a}/trial-{n}/attempt-{m}  cwd={ws}")
            print("  " + " ".join(f'"{c}"' if " " in c else c
                                  for c in build_command(a, ws, exes.get(a))))
        return 0

    failures: list[str] = []

    def work(job):
        t, a, n, m = job
        r = run_attempt(t, a, n, m, runs_dir, args.timeout, exes.get(a))
        if r["status"] != "complete":
            failures.append(r["label"])
            return f"{r['label']}: INCOMPLETE ({r['error']}); evidence kept in {r['dir']}"
        c = r["classification"]
        return (f"{r['label']}: {r['verdict']} ({c['outcome']}, "
                f"{c['failure_source'] or 'ok'}, confirmed={c['confirmed']})")

    with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        futures = [pool.submit(work, j) for j in jobs]
        for fut in futures:
            try:
                print(fut.result(), flush=True)
            except Exception as e:  # keep going; one broken run must not stop the batch
                failures.append(f"{type(e).__name__}: {e}")
                print(f"run failed: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
    if failures:
        print(f"{len(failures)} attempt(s) incomplete or failed: " + "; ".join(failures),
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
