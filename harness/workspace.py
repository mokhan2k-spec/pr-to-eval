"""Throw-away workspaces: a copy of upstream/ with a patch applied."""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

from .tasks import WORK_DIR, Task


class ApplyError(Exception):
    """git apply rejected the patch, or exited 0 without changing what the patch names."""


def remove_tree(path: Path) -> None:
    def handler(func, p, _exc):
        try:
            os.chmod(p, 0o700)
            func(p)
        except OSError:
            pass

    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=handler)
    else:
        shutil.rmtree(path, onerror=lambda f, p, e: handler(f, p, e))


def patch_is_empty(patch: Path) -> bool:
    return not Path(patch).read_bytes().strip()


def _strip1(path: str) -> str:
    """-p1: drop the first path component (a/ or b/)."""
    path = path.split("\t", 1)[0].strip()
    return path.split("/", 1)[1] if "/" in path else path


def patch_paths(patch_text: str) -> list[str]:
    """Paths (relative to the workspace, as `git apply -p1` sees them) that a patch
    creates, edits or deletes."""
    paths: list[str] = []

    def add(p: str) -> None:
        if p and p not in paths:
            paths.append(p)

    old = None
    for line in patch_text.replace("\r\n", "\n").split("\n"):
        if line.startswith("diff --git "):
            m = re.match(r"diff --git a/(.+?) b/(.+)$", line)
            if m:
                add(m.group(2))
            old = None
        elif line.startswith("--- "):
            old = line[4:].split("\t", 1)[0].strip()
        elif line.startswith("+++ ") and old is not None:
            new = line[4:].split("\t", 1)[0].strip()
            add(_strip1(old if new == "/dev/null" else new))
            old = None
    return paths


def _digest(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def apply_patch(ws: Path, patch: Path) -> None:
    """Apply a -p1 patch inside ws. Empty patches are a no-op.

    git apply run in a directory that sits inside another repository (workspaces live
    under <repo>/.work/) can report "Skipped patch" and exit 0 without touching anything.
    So the workspace gets its own throw-away .git for the duration of the apply, and the
    result is verified: every file the patch names must really have changed."""
    patch = Path(patch)
    ws = Path(ws)
    if not patch.exists():
        raise ApplyError(f"patch file not found: {patch.name}")
    if patch_is_empty(patch):
        return
    names = patch_paths(patch.read_text(encoding="utf-8", errors="replace"))
    before = {n: _digest(ws / n) for n in names}
    env = dict(os.environ)
    env["GIT_CEILING_DIRECTORIES"] = str(ws.resolve().parent)
    git_dir = ws / ".git"
    had_git = git_dir.exists()
    try:
        if not had_git:
            init = subprocess.run(["git", "init", "-q"], cwd=ws, env=env, capture_output=True,
                                  text=True, encoding="utf-8", errors="replace")
            if init.returncode != 0:
                raise ApplyError("git init failed: " + (init.stderr or init.stdout).strip())
        proc = subprocess.run(
            ["git", "-c", "core.autocrlf=false", "-c", "core.safecrlf=false", "apply", "-p1",
             "--whitespace=nowarn", str(patch.resolve())],
            cwd=ws, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
    finally:
        if not had_git and git_dir.exists():
            remove_tree(git_dir)
    if proc.returncode != 0:
        raise ApplyError((proc.stderr or proc.stdout).strip() or "git apply failed")
    if not names:
        raise ApplyError("patch names no files: nothing was applied")
    untouched = [n for n in names if _digest(ws / n) == before[n]]
    if untouched:
        note = proc.stderr.strip()
        raise ApplyError("git apply exited 0 but did not change: " + ", ".join(untouched)
                         + (f" ({note})" if note else ""))


def make_workspace(task: Task, patch: Path | None = None, root: Path | None = None) -> Path:
    """Copy upstream/ into <root or .work>/<uuid>/ and apply patch. Raises ApplyError
    (after removing the workspace) if the patch does not apply."""
    root = Path(root) if root else WORK_DIR
    root.mkdir(parents=True, exist_ok=True)
    ws = root / uuid.uuid4().hex
    shutil.copytree(task.upstream_dir, ws)
    if patch is not None:
        try:
            apply_patch(ws, patch)
        except ApplyError:
            remove_tree(ws)
            raise
    return ws
