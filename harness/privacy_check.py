"""Scan the repository (and optionally its whole git history) for personal data.

    python -m harness.privacy_check [--history]

Scans tracked files plus untracked-not-ignored files (`git ls-files`), and with --history
the patch text of `git log -p --all`. Prints `path:line: [rule] matched-text` and exits 1
on any hit not covered by the allowlist in privacy.json:

    [{"path_glob": "LICENSE", "rule": "username", "pattern": "^muhammed$", "reason": "..."}]

An entry is scoped (R22): it allows a hit only when fnmatch(path, path_glob), the detector
that fired is `rule` (a detector name, or a list of them), and the regex `pattern` re.searches
(case-insensitive) the DETECTED VALUE, not the line around it. A line holding an allowed value
and a real one therefore still reports the real one.
`"mode": "exact_line"` is the narrow escape hatch: the pattern is matched against the whole
line instead, must be anchored ^...$ (checked at load; a bad entry fails loudly), and is for one
precisely identified record only. In --history mode a hit is allowed by its file path from the
diff header; non-diff lines (commit headers) have the path "git-log".
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import re
import subprocess
import sys
from pathlib import Path

from .tasks import ROOT, utf8_stdout

ALLOWLIST_PATH = ROOT / "privacy.json"

RULES = {
    "windows-user-path": r"[A-Za-z]:[\\/]Users[\\/]",
    "unix-users-path": r"/Users/",
    "unix-home-path": r"/home/",
    "username": r"muhammed",
    "mail-handle": r"mokhan",
    "email": r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+",
    "employer-1": r"rakader",
    "employer-2": r"kader",
    "phone": r"\+27[0-9 ]{9,}",
    "anthropic-key": r"sk-ant-",
    "github-token": r"ghp_",
    "github-pat": r"github_pat_",
    "aws-key": r"AKIA",
}
COMPILED = {name: re.compile(rx, re.I if name not in ("aws-key", "github-token", "github-pat",
                                                      "anthropic-key") else 0)
            for name, rx in RULES.items()}


MODES = ("value", "exact_line")


def _entry_rules(e: dict) -> list[str]:
    r = e.get("rule")
    return [r] if isinstance(r, str) else list(r) if isinstance(r, list) else []


def validate_entry(e: dict, where: str = "allowlist") -> None:
    """Raise ValueError for an entry that is not strictly scoped."""
    if not isinstance(e, dict):
        raise ValueError(f"{where}: entry is not an object: {e!r}")
    missing = [k for k in ("path_glob", "rule", "pattern", "reason") if k not in e]
    if missing:
        raise ValueError(f"{where}: entry lacks {missing}: {e}")
    if not str(e["reason"]).strip() or not str(e["path_glob"]).strip():
        raise ValueError(f"{where}: path_glob and reason must not be empty: {e}")
    rules = _entry_rules(e)
    unknown = [r for r in rules if r not in COMPILED]
    if not rules or unknown or not all(isinstance(r, str) for r in rules):
        raise ValueError(f"{where}: rule must be one or more detector names {sorted(COMPILED)}, "
                         f"got {e.get('rule')!r}: {e}")
    mode = e.get("mode", "value")
    if mode not in MODES:
        raise ValueError(f"{where}: mode must be one of {MODES}, got {mode!r}: {e}")
    pat = e["pattern"]
    if not isinstance(pat, str) or not pat:
        raise ValueError(f"{where}: pattern must be a non-empty regex: {e}")
    try:
        re.compile(pat)
    except re.error as err:
        raise ValueError(f"{where}: pattern is not a valid regex ({err}): {e}") from err
    if mode == "exact_line" and not (pat.startswith("^") and pat.endswith("$")
                                     and not pat.endswith("\\$")):
        raise ValueError(f"{where}: an exact_line pattern must be anchored ^...$: {e}")


def load_allowlist(path: Path = ALLOWLIST_PATH) -> list[dict]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    for e in data:
        validate_entry(e, str(path))
    return data


def is_allowed(allow: list[dict], path: str, rule: str, matched: str, line: str) -> bool:
    for e in allow:
        if not fnmatch.fnmatch(path, e["path_glob"]) or rule not in _entry_rules(e):
            continue
        if e.get("mode", "value") == "exact_line":
            if re.search(e["pattern"], line):
                return True
        elif re.search(e["pattern"], matched, re.I):
            return True
    return False


def scan_text(path: str, text: str, allow: list[dict], line_offset: int = 0) -> list[tuple]:
    hits = []
    for n, line in enumerate(text.splitlines(), 1):
        for rule, rx in COMPILED.items():
            for m in rx.finditer(line):
                if not is_allowed(allow, path, rule, m.group(0), line):
                    hits.append((path, n + line_offset, rule, m.group(0)))
    return hits


class Scope:
    """What the scan actually inspected, and what it could not inspect (R17). A scan with any
    failure is incomplete and must not pass."""

    def __init__(self):
        self.files_scanned = 0
        self.files_binary = 0
        self.commits_scanned = 0
        self.history_checked = False
        self.no_commits = False
        self.failures: list[str] = []

    def summary(self) -> str:
        hist = ("not scanned (use --history)" if not self.history_checked
                else "no commits (verified)" if self.no_commits
                else f"{self.commits_scanned} commit(s) scanned")
        return (f"scope: {self.files_scanned} file(s) scanned ({self.files_binary} binary skipped), "
                f"history: {hist}, {len(self.failures)} scan failure(s)")


def _git(root: Path, *args: str):
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True)


def tracked_files(root: Path, scope: Scope | None = None) -> list[str]:
    res = _git(root, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
    if res.returncode != 0:
        msg = f"git ls-files failed (exit {res.returncode}): {res.stderr.decode('utf-8', 'replace').strip()[:200]}"
        if scope is None:
            raise RuntimeError(msg)
        scope.failures.append(msg)
        return []
    return sorted({p for p in res.stdout.decode("utf-8", "replace").split("\0") if p})


def scan_files(root: Path, allow: list[dict], scope: Scope | None = None) -> list[tuple]:
    scope = scope if scope is not None else Scope()
    hits = []
    for rel in tracked_files(root, scope):
        p = Path(root) / rel
        try:
            data = p.read_bytes()
        except OSError as e:
            scope.failures.append(f"unreadable file {rel}: {type(e).__name__}: {e}")
            continue
        if b"\0" in data[:8192]:
            scope.files_binary += 1
            continue  # binary
        scope.files_scanned += 1
        hits += scan_text(rel, data.decode("utf-8", errors="replace"), allow)
    return hits


def scan_history(root: Path, allow: list[dict], scope: Scope | None = None) -> list[tuple]:
    scope = scope if scope is not None else Scope()
    scope.history_checked = True
    count = _git(root, "rev-list", "--all", "--count")
    if count.returncode != 0:
        scope.failures.append(f"git rev-list failed (exit {count.returncode}): "
                              f"{count.stderr.decode('utf-8', 'replace').strip()[:200]}")
        return []
    if count.stdout.decode().strip() == "0":
        scope.no_commits = True          # verified: the repository really has no commits
        return []
    res = _git(root, "log", "-p", "--all", "--no-color")
    if res.returncode != 0:
        scope.failures.append(f"git log failed (exit {res.returncode}): "
                              f"{res.stderr.decode('utf-8', 'replace').strip()[:200]}")
        return []
    hits, current = [], "?"
    for n, line in enumerate(res.stdout.decode("utf-8", "replace").splitlines(), 1):
        if line.startswith("commit "):
            scope.commits_scanned += 1
        if line.startswith("diff --git "):
            m = re.match(r"diff --git a/(.+?) b/(.+)$", line)
            current = m.group(2) if m else "?"
            continue
        in_diff = line[:1] in ("+", "-", " ") and bool(line)
        text = line[1:] if in_diff else line        # drop the diff marker so `+@pytest` is not an email
        where = current if in_diff else "git-log"
        for rule, rx in COMPILED.items():
            for m in rx.finditer(text):
                if not is_allowed(allow, where, rule, m.group(0), text):
                    hits.append((f"history:{where}", n, rule, m.group(0)))
    return hits


def main(argv=None) -> int:
    utf8_stdout()
    ap = argparse.ArgumentParser(prog="harness.privacy_check")
    ap.add_argument("--history", action="store_true", help="also scan `git log -p --all`")
    ap.add_argument("--root", default=str(ROOT))
    ap.add_argument("--allowlist", default=str(ALLOWLIST_PATH))
    args = ap.parse_args(argv)
    root = Path(args.root)
    allow = load_allowlist(Path(args.allowlist))
    scope = Scope()
    hits = scan_files(root, allow, scope)
    if args.history:
        hits += scan_history(root, allow, scope)
    for path, line, rule, text in hits:
        print(f"{path}:{line}: [{rule}] {text}")
    for f in scope.failures:
        print(f"SCAN FAILURE: {f}")
    print(f"privacy_check: {len(hits)} unallowlisted hit(s)")
    print(scope.summary())
    if scope.failures:
        print("privacy_check: INCOMPLETE scan (failures above): exit 2")
        return 2
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main())
