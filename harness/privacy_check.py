"""Scan the repository (and optionally its whole git history) for personal data.

    python -m harness.privacy_check [--history]

Scans tracked files plus untracked-not-ignored files (`git ls-files`), and with --history
the patch text of `git log -p --all`. Prints `path:line: [rule] matched-text` and exits 1
on any hit not covered by the allowlist in privacy.json:

    [{"path_glob": "LICENSE", "pattern": "copyright", "reason": "..."}]

An entry allows a hit when fnmatch(path, path_glob) and `pattern` is "*", or the rule
name, or a regex that re.search finds (case-insensitive) in the matched text or its line.
In --history mode a hit is allowed by its file path from the diff header.
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


def load_allowlist(path: Path = ALLOWLIST_PATH) -> list[dict]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    for e in data:
        if not {"path_glob", "pattern", "reason"} <= set(e) or not str(e["reason"]).strip():
            raise ValueError(f"{path}: every entry needs path_glob, pattern and a reason: {e}")
    return data


def is_allowed(allow: list[dict], path: str, rule: str, matched: str, line: str) -> bool:
    for e in allow:
        if not fnmatch.fnmatch(path, e["path_glob"]):
            continue
        pat = e["pattern"]
        if pat == "*" or pat == rule:
            return True
        try:
            if re.search(pat, matched, re.I) or re.search(pat, line, re.I):
                return True
        except re.error:
            pass
    return False


def scan_text(path: str, text: str, allow: list[dict], line_offset: int = 0) -> list[tuple]:
    hits = []
    for n, line in enumerate(text.splitlines(), 1):
        for rule, rx in COMPILED.items():
            for m in rx.finditer(line):
                if not is_allowed(allow, path, rule, m.group(0), line):
                    hits.append((path, n + line_offset, rule, m.group(0)))
    return hits


def tracked_files(root: Path) -> list[str]:
    res = subprocess.run(["git", "-C", str(root), "ls-files", "-z", "--cached", "--others",
                          "--exclude-standard"], capture_output=True, check=True)
    return sorted({p for p in res.stdout.decode("utf-8", "replace").split("\0") if p})


def scan_files(root: Path, allow: list[dict]) -> list[tuple]:
    hits = []
    for rel in tracked_files(root):
        p = Path(root) / rel
        try:
            data = p.read_bytes()
        except OSError:
            continue
        if b"\0" in data[:8192]:
            continue  # binary
        hits += scan_text(rel, data.decode("utf-8", errors="replace"), allow)
    return hits


def scan_history(root: Path, allow: list[dict]) -> list[tuple]:
    res = subprocess.run(["git", "-C", str(root), "log", "-p", "--all", "--no-color"],
                         capture_output=True)
    if res.returncode != 0:  # e.g. no commits yet
        return []
    hits, current = [], "?"
    for n, line in enumerate(res.stdout.decode("utf-8", "replace").splitlines(), 1):
        if line.startswith("diff --git "):
            m = re.match(r"diff --git a/(.+?) b/(.+)$", line)
            current = m.group(2) if m else "?"
            continue
        text = line
        where = current if line[:1] in ("+", "-", " ") and line else "git-log"
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
    hits = scan_files(root, allow)
    if args.history:
        hits += scan_history(root, allow)
    for path, line, rule, text in hits:
        print(f"{path}:{line}: [{rule}] {text}")
    print(f"privacy_check: {len(hits)} unallowlisted hit(s)")
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main())
