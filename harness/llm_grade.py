"""Run each task's LLM grading prompt through the `claude` CLI. Manual, never in CI.

The prompt is piped on stdin (`claude -p` reads it there) because diffs can exceed
the Windows command-line limit.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

from .agent_runs import (Redactor, TRIAL_RE, clean_env, list_attempts, read_json,
                         resolve_exe, sha256_bytes, sha256_file, task_hash)
from .grade import run_process
from .tasks import RUNS_DIR, TASKS_DIR, Task, TaskError, load_tasks, utf8_stdout
from .workspace import remove_tree

MODEL = "sonnet"
TIMEOUT_S = 600


def fill_prompt(template: str, task_md: str, diff: str) -> str:
    return template.replace("{{TASK}}", task_md).replace("{{DIFF}}", diff)


def build_command(exe=None) -> list[str]:
    exe = [exe] if isinstance(exe, str) else list(exe) if exe else ["claude"]
    return exe + ["-p", "--model", MODEL, "--setting-sources", "project",
                  "--strict-mcp-config", "--no-session-persistence",
                  "--permission-mode", "dontAsk", "--output-format", "json"]


def extract_json(text: str):
    """First JSON object in text that has a 'verdict' key, else None."""
    dec = json.JSONDecoder()
    for i, ch in enumerate(text):
        if ch != "{":
            continue
        try:
            obj, _ = dec.raw_decode(text, i)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and "verdict" in obj:
            return obj
    return None


def call_claude(prompt: str, exe=None) -> dict:
    tmp = Path(tempfile.mkdtemp(prefix="pr2e-llm-"))
    try:
        res = run_process(build_command(exe or resolve_exe("claude")), cwd=tmp,
                          env=clean_env(), timeout=TIMEOUT_S, input_text=prompt)
    finally:
        remove_tree(tmp)
    if res.timed_out or res.returncode != 0:
        raise RuntimeError(f"claude failed (exit {res.returncode}, timed_out={res.timed_out}): "
                           f"{res.stderr.strip()[:500]}")
    try:
        envelope = json.loads(res.stdout)
        text = envelope.get("result", "") if isinstance(envelope, dict) else res.stdout
    except json.JSONDecodeError:
        envelope, text = {}, res.stdout
    parsed = extract_json(text)
    if parsed is None:
        raise RuntimeError(f"no JSON verdict in model output: {text[:300]!r}")
    usage = envelope.get("modelUsage") if isinstance(envelope, dict) else None
    out = dict(parsed)
    out["model"] = MODEL
    if usage:
        out["model_ids"] = sorted(usage)
    out["raw_text"] = text
    return out


def _save(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def jobs_for_tasks(tasks: list[Task]):
    for t in tasks:
        for name, _spec in t.sorted_solutions():
            yield t, t.patch_path(name), t.path / "llm_grades" / f"{name}.json"


def final_attempts(runs_dir: Path, task_id: str, all_attempts: bool = False):
    """Attempt dirs of a task: the last attempt of every trial (or all of them)."""
    for tdir in sorted(Path(runs_dir).glob(f"{task_id}/*/trial-*")):
        if not TRIAL_RE.match(tdir.name):
            continue
        attempts = list_attempts(tdir)
        for _n, adir in (attempts if all_attempts else attempts[-1:]):
            yield adir


def bindings(task: Task, patch_bytes: bytes) -> dict:
    return {"patch_sha256": sha256_bytes(patch_bytes), "task_hash": task_hash(task),
            "grader_prompt_sha256": sha256_file(task.path / "grader_prompt.md"), "model": MODEL}


def is_current(existing: dict | None, want: dict) -> bool:
    """A recorded run-grade is reusable only if every bound hash matches and it did not fail."""
    return (bool(existing) and not existing.get("stale")
            and existing.get("status") in ("graded", "not_graded")
            and all(existing.get(k) == v for k, v in want.items()))


def grade_attempt(task: Task, adir: Path, force: bool = False, exe=None, dry_run: bool = False):
    """Write adir/llm_grade.json (always): graded | not_graded (empty patch) | error."""
    out = adir / "llm_grade.json"
    patch = (adir / "final.patch").read_bytes() if (adir / "final.patch").exists() else b""
    want = bindings(task, patch)
    if not force and is_current(read_json(out), want):
        return "skipped (hashes match)"
    if not patch.strip():
        _save(out, {"status": "not_graded", "reason": "empty patch", **want})
        return "not_graded (empty patch)"
    prompt = fill_prompt(task.grader_prompt(), task.task_md(),
                         patch.decode("utf-8", errors="replace"))
    if dry_run:
        return f"[dry-run] {' '.join(build_command())} (prompt on stdin, {len(prompt)} chars) -> {out.name}"
    try:
        result = call_claude(prompt, exe)
        result = Redactor().obj(result)
        _save(out, {"status": "graded", **result, **want})
        return f"graded: {result.get('verdict')}"
    except (RuntimeError, OSError) as e:
        _save(out, {"status": "error", "error": Redactor().obj(str(e))[:500], **want})
        return f"error: {e}"


def main(argv=None) -> int:
    utf8_stdout()
    ap = argparse.ArgumentParser(prog="harness.llm_grade")
    ap.add_argument("--task")
    ap.add_argument("--tasks-dir", default=str(TASKS_DIR))
    ap.add_argument("--runs-dir", default=str(RUNS_DIR))
    ap.add_argument("--runs", action="store_true", help="grade the final attempt of every run instead")
    ap.add_argument("--all-attempts", action="store_true", help="with --runs: also non-final attempts")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    try:
        tasks = load_tasks(Path(args.tasks_dir), args.task)
    except TaskError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    status = 0
    if args.runs:
        for task in tasks:
            for adir in final_attempts(Path(args.runs_dir), task.id, args.all_attempts):
                label = "/".join(adir.parts[-4:])
                msg = grade_attempt(task, adir, args.force, dry_run=args.dry_run)
                print(f"{label}: {msg}")
                if msg.startswith("error"):
                    status = 1
        return status
    for task, patch, out in jobs_for_tasks(tasks):
        label = f"{task.id}/{out.stem}"
        if out.exists() and not args.force:
            print(f"{label}: skipped (already recorded)")
            continue
        prompt = fill_prompt(task.grader_prompt(), task.task_md(),
                             patch.read_text(encoding="utf-8", errors="replace"))
        if args.dry_run:
            print(f"[dry-run] {label}: {' '.join(build_command())}  "
                  f"(prompt on stdin, {len(prompt)} chars) -> {out}")
            continue
        try:
            _save(out, call_claude(prompt))
            print(f"{label}: recorded")
        except (RuntimeError, OSError) as e:
            print(f"{label}: FAILED: {e}", file=sys.stderr)
            status = 1
    return status


if __name__ == "__main__":
    sys.exit(main())
