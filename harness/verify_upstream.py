"""Network check of a task against its upstream PR. Needs internet and an authenticated `gh`.

For every task:
  1. vendored files == upstream at base_sha (byte for byte, and nothing extra);
  2. PR provenance: the PR is merged, merge_sha is the PR's merge commit, and base_sha is
     a parent of merge_sha (task.json "merge_note" / "base_note" explain a deliberate gap);
  3. reference.patch applied to the vendored files gives, for every file it touches, the
     exact bytes of that file at merge_sha (task.json "reference_deviations" lists
     justified exceptions);
  4. every change block of reference.patch appears in the PR's own diff.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

from .tasks import TASKS_DIR, Task, TaskError, load_tasks, utf8_stdout
from .workspace import ApplyError, make_workspace, patch_paths, remove_tree

MIN_NOTE_CHARS = 20
HUNK = re.compile(r"^@@ -\d+(?:,(\d+))? \+\d+(?:,(\d+))? @@")


class PatchParseError(ValueError):
    """The patch text could not be understood well enough to trust the comparison."""


def _header_path(raw: str, prefix: str) -> str:
    path = raw.split("\t", 1)[0].strip()
    return path[len(prefix):] if path.startswith(prefix) else path


def _scan(patch_text: str) -> tuple[dict[str, list[list[str]]], int]:
    """(file -> runs, number of '+++' headers). A run is a maximal block of consecutive
    +/- lines. Understands `diff --git` patches and plain unified diffs, with or without
    a/ b/ prefixes. Hunk line counts decide where a hunk ends, so a removed line that
    starts with '-- ' is never mistaken for a file header."""
    files: dict[str, list[list[str]]] = {}
    current: str | None = None
    old_path: str | None = None
    run: list[str] = []
    headers = 0
    rem_old = rem_new = 0

    def flush():
        nonlocal run
        if run and current is not None:
            files[current].append(run)
        run = []

    for line in patch_text.replace("\r\n", "\n").split("\n"):
        if rem_old > 0 or rem_new > 0:
            if line.startswith("\\"):
                continue
            c = line[:1]
            if c == "+":
                rem_new -= 1
                run.append(line)
            elif c == "-":
                rem_old -= 1
                run.append(line)
            else:  # context line (a blank context line may have lost its space)
                rem_old -= 1
                rem_new -= 1
                flush()
            continue
        if line.startswith("diff --git "):
            flush()
            m = re.match(r"diff --git a/(.+?) b/(.+)$", line)
            current = m.group(2) if m else None
            old_path = None
            if current is not None:
                files.setdefault(current, [])
        elif line.startswith("--- "):
            flush()
            old_path = _header_path(line[4:], "a/")
        elif line.startswith("+++ "):
            flush()
            headers += 1
            new = line[4:].split("\t", 1)[0].strip()
            current = old_path if new == "/dev/null" else _header_path(new, "b/")
            if current is not None and current != "/dev/null":
                files.setdefault(current, [])
            else:
                current = None
        elif line.startswith("@@"):
            flush()
            m = HUNK.match(line)
            if m:
                rem_old = int(m.group(1)) if m.group(1) is not None else 1
                rem_new = int(m.group(2)) if m.group(2) is not None else 1
        else:
            flush()
    flush()
    return files, headers


def parse_patch_runs(patch_text: str) -> dict[str, list[list[str]]]:
    """file -> list of runs, for a whole patch. Raises PatchParseError for an empty
    parse, or one that saw fewer files than the patch has '+++' headers."""
    files, headers = _scan(patch_text)
    if not files:
        raise PatchParseError("the patch parsed to zero files")
    if len(files) < headers:
        raise PatchParseError(f"parsed {len(files)} files but the patch has {headers} "
                              "'+++' headers")
    return files


def runs_of_github_patch(patch: str) -> list[list[str]]:
    """Runs of GitHub's per-file 'patch' text (hunks only, no file headers)."""
    files, _ = _scan("diff --git a/x b/x\n" + patch)
    return files.get("x", [])


def run_in(run: list[str], upstream_runs: list[list[str]]) -> bool:
    n = len(run)
    for u in upstream_runs:
        for i in range(len(u) - n + 1):
            if u[i:i + n] == run:
                return True
    return False


def missing_hunks(reference_patch: str, upstream_files: dict[str, list[list[str]]]) -> list[str]:
    problems = []
    for path, runs in parse_patch_runs(reference_patch).items():
        if path not in upstream_files:
            problems.append(f"{path}: not changed in the upstream PR")
            continue
        for run in runs:
            if not run_in(run, upstream_files[path]):
                problems.append(f"{path}: change block not in upstream patch: {run[:3]}")
    return problems


# --------------------------------------------------------------------------
# Network (all of it goes through these three functions; tests replace them)
# --------------------------------------------------------------------------

def fetch_raw(repo: str, sha: str, path: str) -> bytes:
    url = f"https://raw.githubusercontent.com/{repo}/{sha}/{path}"
    req = urllib.request.Request(url, headers={"User-Agent": "pr-to-eval"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read()


def gh_json(endpoint: str):
    res = subprocess.run(["gh", "api", endpoint], capture_output=True, text=True,
                         encoding="utf-8", check=True)
    return json.loads(res.stdout)


def fetch_pr_files(repo: str, pr: int) -> dict[str, list[list[str]]]:
    res = subprocess.run(["gh", "api", f"repos/{repo}/pulls/{pr}/files", "--paginate"],
                         capture_output=True, text=True, encoding="utf-8", check=True)
    dec, text, i, out = json.JSONDecoder(), res.stdout, 0, {}
    while i < len(text):  # --paginate prints one JSON array per page
        while i < len(text) and text[i].isspace():
            i += 1
        if i >= len(text):
            break
        page, i = dec.raw_decode(text, i)
        for f in page:
            out[f["filename"]] = runs_of_github_patch(f.get("patch", ""))
    return out


# --------------------------------------------------------------------------
# Checks
# --------------------------------------------------------------------------

def _note_ok(value) -> bool:
    return isinstance(value, str) and len("".join(value.split())) >= MIN_NOTE_CHARS


def check_provenance(task: Task, gh=None) -> tuple[list[str], list[str]]:
    """(problems, notes): PR merged, merge_sha is its merge commit, base_sha is a parent
    of merge_sha. A gap is accepted only with a justification in task.json."""
    gh = gh or gh_json
    up = task.upstream
    problems: list[str] = []
    notes: list[str] = []
    pr = gh(f"repos/{up['repo']}/pulls/{up['pr']}")
    if pr.get("merged") is not True:
        problems.append(f"PR #{up['pr']} is not merged")
    if pr.get("merge_commit_sha") != up["merge_sha"]:
        note = task.data.get("merge_note")
        if _note_ok(note):
            notes.append(f"merge_sha differs from the PR's merge_commit_sha "
                         f"({pr.get('merge_commit_sha')}); merge_note: {note}")
        else:
            problems.append(f"merge_sha {up['merge_sha'][:10]} != PR merge_commit_sha "
                            f"{str(pr.get('merge_commit_sha'))[:10]} and task.json has no "
                            f"merge_note (>= {MIN_NOTE_CHARS} chars) explaining it")
    commit = gh(f"repos/{up['repo']}/commits/{up['merge_sha']}")
    parents = [p.get("sha") for p in commit.get("parents", [])]
    if up["base_sha"] not in parents:
        note = task.data.get("base_note")
        if _note_ok(note):
            notes.append(f"base_sha is not a parent of merge_sha; base_note: {note}")
        else:
            problems.append(f"base_sha {up['base_sha'][:10]} is not a parent of merge_sha "
                            f"{up['merge_sha'][:10]} (parents: "
                            f"{[str(p)[:10] for p in parents]}) and task.json has no base_note "
                            f"(>= {MIN_NOTE_CHARS} chars) explaining it")
    return problems, notes


def compare_reference_bytes(task: Task, raw=None) -> tuple[list[str], list[str]]:
    """Apply reference.patch to the vendored upstream and compare every touched file,
    byte for byte, with the raw file at merge_sha."""
    raw = raw or fetch_raw
    up = task.upstream
    deviations = task.data.get("reference_deviations") or {}
    problems: list[str] = []
    notes: list[str] = []
    ref = task.patch_path("reference")
    try:
        paths = patch_paths(ref.read_text(encoding="utf-8"))
        ws = make_workspace(task, ref)
    except (ApplyError, OSError) as e:
        return [f"reference.patch does not apply to the vendored upstream: {e}"], notes
    try:
        if not paths:
            return ["reference.patch names no files"], notes
        for rel in paths:
            local = ws / rel
            try:
                remote = raw(up["repo"], up["merge_sha"], rel)
            except urllib.error.HTTPError as e:
                if e.code == 404 and not local.exists():
                    continue  # deleted by the patch and absent upstream
                problems.append(f"{rel}: could not fetch from upstream at merge_sha: {e}")
                continue
            except (urllib.error.URLError, OSError) as e:
                problems.append(f"{rel}: could not fetch from upstream at merge_sha: {e}")
                continue
            same = local.is_file() and local.read_bytes() == remote
            declared = rel in deviations
            if same and declared:
                problems.append(f"{rel}: listed in reference_deviations but matches merge_sha "
                                "byte for byte (stale entry)")
            elif not same and declared:
                if _note_ok(deviations[rel]):
                    notes.append(f"{rel}: deviates from merge_sha, justified: {deviations[rel]}")
                else:
                    problems.append(f"{rel}: reference_deviations justification is shorter than "
                                    f"{MIN_NOTE_CHARS} non-blank characters")
            elif not same:
                problems.append(f"{rel}: reference.patch result differs from the file at "
                                f"merge_sha {up['merge_sha'][:10]} (add a justified "
                                "reference_deviations entry or fix the patch)")
        for rel in deviations:
            if rel not in paths:
                problems.append(f"{rel}: in reference_deviations but not touched by "
                                "reference.patch")
    finally:
        remove_tree(ws)
    return problems, notes


def verify_task(task: Task) -> tuple[list[str], list[str]]:
    up = task.upstream
    problems: list[str] = []
    notes: list[str] = []
    for rel in up["vendored_files"]:
        local = task.upstream_dir / rel
        if not local.exists():
            problems.append(f"{rel}: listed in vendored_files but missing locally")
            continue
        try:
            remote = fetch_raw(up["repo"], up["base_sha"], rel)
        except (urllib.error.URLError, OSError) as e:
            problems.append(f"{rel}: download failed: {e}")
            continue
        if remote != local.read_bytes():
            problems.append(f"{rel}: differs from upstream at {up['base_sha'][:10]}")
    actual = {p.relative_to(task.upstream_dir).as_posix()
              for p in task.upstream_dir.rglob("*") if p.is_file()}
    for extra in sorted(actual - set(up["vendored_files"])):
        problems.append(f"{extra}: present in upstream/ but not in vendored_files")
    try:
        p, n = check_provenance(task)
        problems += p
        notes += n
    except (subprocess.CalledProcessError, FileNotFoundError, json.JSONDecodeError) as e:
        problems.append(f"PR provenance lookup failed: {e}")
    p, n = compare_reference_bytes(task)
    problems += p
    notes += n
    try:
        files = fetch_pr_files(up["repo"], up["pr"])
        ref = task.patch_path("reference").read_text(encoding="utf-8")
        problems += missing_hunks(ref, files)
    except PatchParseError as e:
        problems.append(f"reference.patch: {e}")
    except (subprocess.CalledProcessError, FileNotFoundError, json.JSONDecodeError) as e:
        problems.append(f"PR files lookup failed: {e}")
    return problems, notes


def main(argv=None) -> int:
    utf8_stdout()
    ap = argparse.ArgumentParser(prog="harness.verify_upstream")
    ap.add_argument("--task")
    ap.add_argument("--tasks-dir", default=str(TASKS_DIR))
    args = ap.parse_args(argv)
    try:
        tasks = load_tasks(Path(args.tasks_dir), args.task)
    except TaskError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    status = 0
    for t in tasks:
        problems, notes = verify_task(t)
        if problems:
            status = 1
            print(f"FAIL {t.id}")
            for p in problems:
                print(f"  - {p}")
        else:
            print(f"OK   {t.id}: {len(t.upstream['vendored_files'])} vendored files match "
                  f"{t.upstream['base_sha'][:10]}; PR #{t.upstream['pr']} merged, provenance "
                  f"checked; reference.patch result is byte-identical to merge_sha "
                  f"{t.upstream['merge_sha'][:10]} (or justified); its hunks are in the PR diff")
        for n in notes:
            print(f"  note: {n}")
    return status


if __name__ == "__main__":
    sys.exit(main())
