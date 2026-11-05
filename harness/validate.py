"""Grade every solution of every task, compare with expectations, render the table."""
from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import readme
from .grade import grade_patch, run_hidden_tests
from .tasks import (CORPUS_PATH, TASKS_DIR, Task, TaskError, load_tasks, utf8_stdout)
from .workspace import ApplyError, make_workspace, remove_tree

LANGUAGES = ("python", "typescript")

HEADER = ("| Task | Lang | Upstream PR | Solution | Kind | Hidden tests | Shell checks "
          "| LLM grader (recorded) | Verdict | Expected | Match |")
SEP = "|" + "---|" * 11


def failing_ids(result: dict) -> set[str]:
    return set(result["tests"]["failed"]) | set(result["checks"]["failed"])


def matches(spec: dict, result: dict) -> bool:
    """A row matches only when an expect-pass solution is PASS, or an expect-fail
    solution is FAIL with every expect_failing id among the failed ids. ERROR never
    matches."""
    if spec["expect"] == "pass":
        return result["verdict"] == "PASS"
    if result["verdict"] != "FAIL":
        return False
    return set(spec.get("expect_failing", [])) <= failing_ids(result)


def reference_id_problem(task: Task, result: dict) -> str | None:
    """The reference run must produce exactly the ids listed in expected_tests.json
    (ids are unique, so equal sets <=> nothing missing and total == len(expected))."""
    expected = task.expected_tests()
    if expected is None:
        return "expected_tests.json is missing"
    tests = result["tests"]
    if tests["missing"] or tests["total"] != len(expected):
        return (f"reference ran {tests['total']} tests, expected_tests.json lists "
                f"{len(expected)}; missing: {tests['missing']}")
    return None


def _grade_one(job):
    task, name, spec, keep = job
    result = grade_patch(task, task.patch_path(name), keep=keep)
    ref_problem = reference_id_problem(task, result) \
        if spec["kind"] == "reference" and result["verdict"] == "PASS" else None
    llm = task.llm_grade(name)
    llm_verdict = llm.get("verdict", "—") if isinstance(llm, dict) else "—"
    up = task.upstream
    return {
        "task": task.id, "lang": task.language,
        "pr": f"[#{up['pr']}]({up['pr_url']})",
        "solution": name, "kind": spec["kind"],
        "hidden_tests": f"{result['tests']['passed']}/{result['tests']['total']}",
        "shell_checks": f"{result['checks']['passed']}/{result['checks']['total']}",
        "llm": llm_verdict, "verdict": result["verdict"],
        "expected": spec["expect"].upper(),
        "match": matches(spec, result) and ref_problem is None, "result": result,
        "ref_problem": ref_problem,
        "expect_failing": spec.get("expect_failing", []),
    }


def collect_rows(tasks: list[Task], workers: int = 4, keep: bool = False) -> list[dict]:
    jobs = [(t, n, s, keep) for t in tasks for n, s in t.sorted_solutions()]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(_grade_one, jobs))  # map keeps the sorted job order


def render_table(rows: list[dict]) -> str:
    lines = [HEADER, SEP]
    for r in rows:
        lines.append(
            f"| {r['task']} | {r['lang']} | {r['pr']} | {r['solution']} | {r['kind']} "
            f"| {r['hidden_tests']} | {r['shell_checks']} | {r['llm']} | {r['verdict']} "
            f"| {r['expected']} | {'yes' if r['match'] else 'NO'} |")
    ok = sum(1 for r in rows if r["match"])
    lines += ["", f"**{ok}/{len(rows)} solutions match their expected verdict.**"]
    return "\n".join(lines)


def explain_mismatches(rows: list[dict]) -> list[str]:
    out = []
    for r in rows:
        if r["match"]:
            continue
        res = r["result"]
        msg = f"MISMATCH {r['task']}/{r['solution']}: expected {r['expected']}, got {r['verdict']}"
        if res.get("apply_error"):
            msg += f"; apply_error: {res['apply_error']}"
        if r.get("ref_problem"):
            msg += f"; {r['ref_problem']}"
        if r["verdict"] == "ERROR":
            msg += (f"; INFRASTRUCTURE ERROR (not a solution verdict): runner exit "
                    f"{res['runner']['exit']}, junit_ok={res['runner']['junit_ok']}, "
                    f"diagnostics tail:\n{res['runner']['diag_tail']}")
        elif r["expected"] == "PASS":
            msg += f"; failing: {sorted(failing_ids(res))}"
        else:
            missing = sorted(set(r["expect_failing"]) - failing_ids(res))
            msg += f"; expected-to-fail but did not: {missing}"
        out.append(msg)
    return out


# --------------------------------------------------------------------------
# expected_tests.json
# --------------------------------------------------------------------------

def write_expected(task: Task) -> list[str]:
    """Run the reference solution and write the sorted test ids it produced. Refuses
    to write from a run that is not clean."""
    ws = make_workspace(task, task.patch_path("reference"))
    try:
        run = run_hidden_tests(task, ws)
    finally:
        remove_tree(ws)
    j = run["junit"]
    if not run["started"] or run["timed_out"] or not j["ok"] or run["exit"] != 0:
        raise TaskError(f"{task.id}: reference run is not clean (exit {run['exit']}, "
                        f"timed_out={run['timed_out']}, junit_ok={j['ok']}):\n{run['diag_tail']}")
    if j["failed"] or j["skipped"] or not j["ids"]:
        raise TaskError(f"{task.id}: reference run has failed={j['failed']} "
                        f"skipped={j['skipped']} total={j['total']}")
    ids = sorted(set(j["ids"]))
    if len(ids) != len(j["ids"]):
        raise TaskError(f"{task.id}: the reference run has duplicate test ids")
    with open(task.expected_tests_path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(ids, f, indent=2)
        f.write("\n")
    return ids


# --------------------------------------------------------------------------
# Corpus gate
# --------------------------------------------------------------------------

def _json_or_none(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def check_corpus(tasks_dir: Path = TASKS_DIR, corpus_path: Path = CORPUS_PATH) -> list[str]:
    """Problems that make the production task set unfit to publish (empty list = OK)."""
    tasks_dir = Path(tasks_dir)
    corpus = _json_or_none(Path(corpus_path))
    if not isinstance(corpus, dict) or not isinstance(corpus.get("tasks"), list):
        return [f"{Path(corpus_path).name}: missing or not an object with a 'tasks' list"]
    problems: list[str] = []
    entries = [t for t in corpus["tasks"] if isinstance(t, dict) and "id" in t]
    if len(entries) != len(corpus["tasks"]):
        problems.append("corpus.json: every task entry needs an 'id'")
    ids = [t["id"] for t in entries]
    if len(set(ids)) != len(ids):
        problems.append("corpus.json: duplicate task ids")
    langs = {t.get("language") for t in entries}
    for lang in LANGUAGES:
        if lang not in langs:
            problems.append(f"corpus.json: no {lang} task (need at least one of each language)")
    min_flawed = corpus.get("min_flawed", 3)
    min_alt = corpus.get("min_alt", 1)

    present = sorted(p.name for p in tasks_dir.iterdir() if p.is_dir()) \
        if tasks_dir.is_dir() else []
    for name in present:
        if name not in ids:
            problems.append(f"tasks/{name}: not in corpus.json (add it or remove the directory)")

    for entry in entries:
        tid = entry["id"]
        tdir = tasks_dir / tid
        where = f"{tid}:"
        if not tdir.is_dir():
            problems.append(f"{where} no task directory")
            continue
        data = _json_or_none(tdir / "task.json")
        if not isinstance(data, dict):
            problems.append(f"{where} task.json missing or invalid")
            continue
        if data.get("language") != entry.get("language"):
            problems.append(f"{where} language is '{data.get('language')}' in task.json but "
                            f"'{entry.get('language')}' in corpus.json")
        mut = data.get("mutation")
        if not (isinstance(mut, dict) and mut.get("targets")):
            problems.append(f"{where} no mutation config (task.json 'mutation.targets')")
        for fname in ("expected_tests.json", "TASK.md", "checks.sh", "grader_prompt.md"):
            if not (tdir / fname).is_file():
                problems.append(f"{where} {fname} is missing")
        if (tdir / "expected_tests.json").is_file():
            exp = _json_or_none(tdir / "expected_tests.json")
            if not (isinstance(exp, list) and exp):
                problems.append(f"{where} expected_tests.json must be a non-empty list")
        sols = _json_or_none(tdir / "solutions" / "solutions.json")
        if not isinstance(sols, dict):
            problems.append(f"{where} solutions/solutions.json missing or invalid")
            continue
        kinds = {"reference": 0, "flawed": 0, "alt": 0}
        for name, spec in sols.items():
            spec = spec if isinstance(spec, dict) else {}
            kind = spec.get("kind")
            if kind in kinds:
                kinds[kind] += 1
            if not (tdir / "solutions" / f"{name}.patch").is_file():
                problems.append(f"{where} patch file solutions/{name}.patch is missing")
            if kind == "flawed":
                ef = spec.get("expect_failing")
                if not (isinstance(ef, list) and ef):
                    problems.append(f"{where} flawed solution '{name}' has no expect_failing")
            if kind == "alt":
                desc = spec.get("description")
                if not (isinstance(desc, str) and desc.strip()):
                    problems.append(f"{where} alt solution '{name}' has no description")
        if kinds["reference"] != 1:
            problems.append(f"{where} needs exactly one reference solution, "
                            f"has {kinds['reference']}")
        if kinds["flawed"] < min_flawed:
            problems.append(f"{where} needs at least {min_flawed} flawed solutions, "
                            f"has {kinds['flawed']}")
        if kinds["alt"] < min_alt:
            problems.append(f"{where} needs at least {min_alt} alt solution(s), "
                            f"has {kinds['alt']}")
    return problems


def main(argv=None) -> int:
    utf8_stdout()
    ap = argparse.ArgumentParser(prog="harness.validate")
    ap.add_argument("--task", help="only this task id")
    ap.add_argument("--tasks-dir", default=str(TASKS_DIR))
    ap.add_argument("--check-corpus", action="store_true",
                    help="fail unless the task set meets the corpus.json gate")
    ap.add_argument("--corpus", default=str(CORPUS_PATH))
    ap.add_argument("--write-expected", action="store_true",
                    help="regenerate expected_tests.json from each task's reference run")
    ap.add_argument("--check-readme", action="store_true")
    ap.add_argument("--write-readme", action="store_true")
    ap.add_argument("--readme", default=str(readme.README))
    ap.add_argument("--json", metavar="PATH", help="write full results as JSON")
    ap.add_argument("--keep", action="store_true", help="keep workspaces under .work/")
    args = ap.parse_args(argv)
    if (args.check_readme or args.write_readme) and args.task:
        print("--task cannot be combined with README flags", file=sys.stderr)
        return 2
    if args.check_corpus:
        problems = check_corpus(Path(args.tasks_dir), Path(args.corpus))
        if problems:
            print("CORPUS GATE FAILED:", file=sys.stderr)
            for p in problems:
                print(f"  - {p}", file=sys.stderr)
            return 1
        print("corpus gate: OK")
    try:
        tasks = load_tasks(Path(args.tasks_dir), args.task)
    except TaskError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    if args.write_expected:
        for t in tasks:
            try:
                ids = write_expected(t)
            except (TaskError, ApplyError) as e:
                print(f"error: {e}", file=sys.stderr)
                return 1
            print(f"{t.id}: wrote {len(ids)} test ids to expected_tests.json")
        return 0
    rows = collect_rows(tasks, keep=args.keep)
    table = render_table(rows)
    print(table)
    if args.json:
        Path(args.json).write_text(json.dumps(rows, indent=2), encoding="utf-8")
    status = 0
    for line in explain_mismatches(rows):
        print(line, file=sys.stderr)
        status = 1
    if args.write_readme:
        readme.write_block(readme.VALIDATION, table, Path(args.readme))
    elif args.check_readme:
        if not readme.check_block(readme.VALIDATION, table, Path(args.readme)):
            print("README validation table is out of date; run `make readme`", file=sys.stderr)
            status = 1
    return status


if __name__ == "__main__":
    sys.exit(main())
