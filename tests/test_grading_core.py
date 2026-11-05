"""Tests for the hardened grading core: verdict rules, patch apply, corpus gate,
mutation gate, upstream provenance. Fast, no network."""
import json
import shutil
import subprocess
import urllib.error
from pathlib import Path

import pytest

from harness import grade, mutate, validate, verify_upstream
from harness.tasks import TaskError, load_task, load_tasks
from harness.workspace import ApplyError, make_workspace, patch_paths, remove_tree

IDS = ["hidden_tests.test_hidden::test_clamp_below",
       "hidden_tests.test_hidden::test_clamp_above",
       "hidden_tests.test_hidden::test_clamp_inside"]


def copy_task(fixtures_dir, dest_root, name="tiny-python-1"):
    dst = Path(dest_root) / name
    shutil.copytree(fixtures_dir / "tiny-python-1", dst)
    return dst


def junit_xml(cases):
    """cases: list of (id, state) with state in ok/fail/error/skip."""
    out = ["<testsuites><testsuite>"]
    for cid, state in cases:
        cls, name = cid.split("::")
        body = {"ok": "", "fail": "<failure/>", "error": "<error/>", "skip": "<skipped/>"}[state]
        out.append(f'<testcase classname="{cls}" name="{name}">{body}</testcase>')
    out.append("</testsuite></testsuites>")
    return "".join(out)


def scripted_task(fixtures_dir, tmp_path, xml, exit_code, expected=IDS, extra=""):
    """A copy of the tiny task whose test command just writes `xml` and exits."""
    d = copy_task(fixtures_dir, tmp_path)
    t = load_task(d)
    script = (f"import sys;{extra}"
              + (f"open(sys.argv[1],'w').write({xml!r});" if xml is not None else "")
              + f"sys.exit({exit_code})")
    t.data["test_cmd"] = ["python", "-c", script, "{junit}"]
    (d / "expected_tests.json").write_text(json.dumps(sorted(expected)))
    return t


def grade_scripted(task, tmp_path):
    ws = make_workspace(task, task.patch_path("reference"), root=tmp_path / "ws")
    try:
        return grade.grade(task, ws)
    finally:
        remove_tree(ws)


# --- verdict rules (R01) -----------------------------------------------------

def test_clean_run_passes(fixtures_dir, tmp_path):
    t = scripted_task(fixtures_dir, tmp_path, junit_xml([(i, "ok") for i in IDS]), 0)
    r = grade_scripted(t, tmp_path)
    assert r["verdict"] == "PASS"
    assert r["runner"]["exit"] == 0 and r["runner"]["junit_ok"] and not r["runner"]["timed_out"]
    assert r["tests"] == {"total": 3, "passed": 3, "failed": [], "skipped": [], "missing": []}


def test_junit_missing_is_error(fixtures_dir, tmp_path):
    t = scripted_task(fixtures_dir, tmp_path, None, 0)
    r = grade_scripted(t, tmp_path)
    assert r["verdict"] == "ERROR" and r["runner"]["junit_ok"] is False


def test_junit_unparseable_is_error(fixtures_dir, tmp_path):
    t = scripted_task(fixtures_dir, tmp_path, "<testsuites><oops", 0)
    assert grade_scripted(t, tmp_path)["verdict"] == "ERROR"


def test_nonzero_exit_with_clean_junit_is_error(fixtures_dir, tmp_path):
    t = scripted_task(fixtures_dir, tmp_path, junit_xml([(i, "ok") for i in IDS]), 3)
    r = grade_scripted(t, tmp_path)
    assert r["verdict"] == "ERROR" and r["runner"]["exit"] == 3


def test_zero_exit_with_failures_is_error(fixtures_dir, tmp_path):
    cases = [(IDS[0], "fail"), (IDS[1], "ok"), (IDS[2], "ok")]
    t = scripted_task(fixtures_dir, tmp_path, junit_xml(cases), 0)
    assert grade_scripted(t, tmp_path)["verdict"] == "ERROR"


def test_runner_that_cannot_start_is_error(fixtures_dir, tmp_path):
    t = scripted_task(fixtures_dir, tmp_path, None, 0)
    t.data["test_cmd"] = ["definitely-not-a-real-binary-xyz", "{junit}"]
    r = grade_scripted(t, tmp_path)
    assert r["verdict"] == "ERROR" and r["runner"]["exit"] is None


def test_real_failure_is_fail_not_error(fixtures_dir, tmp_path):
    cases = [(IDS[0], "fail"), (IDS[1], "ok"), (IDS[2], "ok")]
    t = scripted_task(fixtures_dir, tmp_path, junit_xml(cases), 1)
    r = grade_scripted(t, tmp_path)
    assert r["verdict"] == "FAIL" and r["tests"]["failed"] == [IDS[0]]


def test_skipped_case_is_fail(fixtures_dir, tmp_path):
    cases = [(IDS[0], "skip"), (IDS[1], "ok"), (IDS[2], "ok")]
    t = scripted_task(fixtures_dir, tmp_path, junit_xml(cases), 0)
    r = grade_scripted(t, tmp_path)
    assert r["verdict"] == "FAIL"
    assert r["tests"]["skipped"] == [IDS[0]]
    assert f"skipped::{IDS[0]}" in r["tests"]["failed"]
    assert r["tests"]["passed"] == 2


def test_missing_expected_id_is_fail(fixtures_dir, tmp_path):
    cases = [(IDS[0], "ok"), (IDS[1], "ok")]
    t = scripted_task(fixtures_dir, tmp_path, junit_xml(cases), 0)
    r = grade_scripted(t, tmp_path)
    assert r["verdict"] == "FAIL"
    assert r["tests"]["missing"] == [IDS[2]]
    assert f"missing::{IDS[2]}" in r["tests"]["failed"]


def test_zero_tests_is_fail(fixtures_dir, tmp_path):
    t = scripted_task(fixtures_dir, tmp_path, junit_xml([]), 0, expected=[])
    assert grade_scripted(t, tmp_path)["verdict"] == "FAIL"


def test_missing_expected_file_is_error(fixtures_dir, tmp_path):
    t = scripted_task(fixtures_dir, tmp_path, junit_xml([(i, "ok") for i in IDS]), 0)
    t.expected_tests_path.unlink()
    r = grade_scripted(t, tmp_path)
    assert r["verdict"] == "ERROR" and "expected_tests.json" in r["runner"]["diag_tail"]


def test_failing_check_turns_pass_into_fail(fixtures_dir, tmp_path):
    t = scripted_task(fixtures_dir, tmp_path, junit_xml([(i, "ok") for i in IDS]), 0)
    (t.path / "checks.sh").write_text('echo "FAIL always: nope"\nexit 1\n')
    r = grade_scripted(t, tmp_path)
    assert r["verdict"] == "FAIL" and r["checks"]["failed"] == ["check:always"]


def test_timeout_is_a_solution_fail(fixtures_dir, tmp_path):
    t = scripted_task(fixtures_dir, tmp_path, None, 0, extra="import time;time.sleep(30);")
    t.data["timeout_s"] = 1
    r = grade_scripted(t, tmp_path)
    assert r["verdict"] == "FAIL"
    assert r["tests"]["failed"] == ["<timeout>"] and r["runner"]["timed_out"] is True


def test_diag_tail_is_scrubbed_and_bounded(fixtures_dir, tmp_path):
    extra = "import os;print(os.getcwd());print(os.path.expanduser('~'));print('x'*6000);"
    t = scripted_task(fixtures_dir, tmp_path, junit_xml([(i, "ok") for i in IDS]), 0, extra=extra)
    ws = make_workspace(t, t.patch_path("reference"), root=tmp_path / "ws")
    try:
        r = grade.grade(t, ws)
        tail = r["runner"]["diag_tail"]
        assert len(tail) <= 4000
        assert str(ws) not in tail and ws.as_posix() not in tail
        assert grade.scrub(f"{ws}/a and {Path.home()}/b", ws) == "<WS>/a and <HOME>/b"
    finally:
        remove_tree(ws)


def test_empty_patch_shape(task):
    r = grade.grade_patch(task, None)
    assert r["verdict"] == "FAIL" and r["tests"]["failed"] == ["<empty-patch>"]
    assert set(r) == {"verdict", "tests", "checks", "runner"}
    assert set(r["runner"]) == {"exit", "timed_out", "junit_ok", "diag_tail"}


# --- expected_tests.json -------------------------------------------------------

def test_write_expected_and_reference_exactness(fixtures_dir, tmp_path):
    d = copy_task(fixtures_dir, tmp_path)
    (d / "expected_tests.json").unlink()
    t = load_task(d)
    assert t.expected_tests() is None
    assert validate.write_expected(t) == sorted(IDS)
    assert json.loads((d / "expected_tests.json").read_text()) == sorted(IDS)
    res = grade.grade_patch(t, t.patch_path("reference"))
    assert res["verdict"] == "PASS" and validate.reference_id_problem(t, res) is None
    # an extra id listed that the run does not produce, or one the run adds: not equal
    (d / "expected_tests.json").write_text(json.dumps(sorted(IDS[:2])))
    assert validate.reference_id_problem(t, res) is not None
    (d / "expected_tests.json").write_text(json.dumps(sorted(IDS + ["x::y"])))
    res2 = grade.grade_patch(t, t.patch_path("reference"))
    assert res2["verdict"] == "FAIL" and res2["tests"]["missing"] == ["x::y"]


def test_validate_reference_row_fails_when_ids_differ(fixtures_dir, tmp_path):
    d = tmp_path / "tasks"
    copy_task(fixtures_dir, d)
    (d / "tiny-python-1" / "expected_tests.json").write_text(json.dumps(sorted(IDS[:2])))
    assert validate.main(["--tasks-dir", str(d)]) == 1


# --- matches / ERROR ---------------------------------------------------------

def res(verdict, failed=(), checks=()):
    return {"verdict": verdict, "tests": {"failed": list(failed)},
            "checks": {"failed": list(checks)}}


def test_error_never_matches():
    assert not validate.matches({"expect": "pass"}, res("ERROR"))
    assert not validate.matches({"expect": "fail", "expect_failing": []}, res("ERROR", ["a"]))
    assert validate.matches({"expect": "fail", "expect_failing": ["a"]}, res("FAIL", ["a", "b"]))


def test_validate_exits_nonzero_and_prints_diagnostics_on_error(fixtures_dir, tmp_path, capsys):
    d = tmp_path / "tasks"
    copy_task(fixtures_dir, d)
    tj = d / "tiny-python-1" / "task.json"
    data = json.loads(tj.read_text())
    data["test_cmd"] = ["python", "-c", "print('infra boom')"]  # no junit written
    tj.write_text(json.dumps(data))
    assert validate.main(["--tasks-dir", str(d)]) == 1
    err = capsys.readouterr().err
    assert "INFRASTRUCTURE ERROR" in err and "infra boom" in err


# --- patch apply (T1) ----------------------------------------------------------

def test_patch_paths_formats():
    git = "diff --git a/x/f.py b/x/f.py\n--- a/x/f.py\n+++ b/x/f.py\n@@ -1 +1 @@\n-a\n+b\n"
    plain = "--- a/x/f.py\n+++ b/x/f.py\n@@ -1 +1 @@\n-a\n+b\n"
    new = "--- /dev/null\n+++ b/x/new.py\n@@ -0,0 +1 @@\n+a\n"
    gone = "--- a/x/old.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-a\n"
    assert patch_paths(git) == ["x/f.py"] and patch_paths(plain) == ["x/f.py"]
    assert patch_paths(new) == ["x/new.py"] and patch_paths(gone) == ["x/old.py"]


def test_apply_inside_a_nested_git_repo(task, tmp_path):
    """Workspaces live under <repo>/.work/: git apply there must really apply."""
    repo = tmp_path / "outer"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / ".gitignore").write_text(".work/\n")
    ws = make_workspace(task, task.patch_path("reference"), root=repo / ".work")
    try:
        assert "value > high" in (ws / "tiny" / "mathx.py").read_text()
        assert not (ws / ".git").exists()  # the throw-away .git is gone again
    finally:
        remove_tree(ws)


def test_apply_that_changes_nothing_raises(task, tmp_path):
    noop = tmp_path / "noop.patch"
    noop.write_bytes("diff --git a/tiny/mathx.py b/tiny/mathx.py\n--- a/tiny/mathx.py\n"
                    "+++ b/tiny/mathx.py\n@@ -3,3 +3,3 @@\n     if value < low:\n-        return low\n+        return low\n"
                    "     return value\n".encode())
    with pytest.raises(ApplyError, match="did not change"):
        make_workspace(task, noop, root=tmp_path / "ws")


# --- corpus gate (R07) -----------------------------------------------------------

def build_corpus(fixtures_dir, tmp_path):
    tasks = tmp_path / "tasks"
    copy_task(fixtures_dir, tasks, "tiny-python-1")
    shutil.copytree(fixtures_dir / "tiny-python-1", tasks / "tiny-ts-1")
    tj = tasks / "tiny-ts-1" / "task.json"
    data = json.loads(tj.read_text())
    data.update(id="tiny-ts-1", language="typescript")
    tj.write_text(json.dumps(data))
    corpus = tmp_path / "corpus.json"
    corpus.write_text(json.dumps({
        "tasks": [{"id": "tiny-python-1", "language": "python"},
                  {"id": "tiny-ts-1", "language": "typescript"}],
        "min_flawed": 2, "min_alt": 1}))
    return tasks, corpus


def sols(tasks, tid="tiny-python-1"):
    p = tasks / tid / "solutions" / "solutions.json"
    return p, json.loads(p.read_text())


def test_corpus_gate_passes_on_a_complete_set(fixtures_dir, tmp_path):
    tasks, corpus = build_corpus(fixtures_dir, tmp_path)
    assert validate.check_corpus(tasks, corpus) == []


def problems_for(fixtures_dir, tmp_path, mutate_fn):
    tasks, corpus = build_corpus(fixtures_dir, tmp_path)
    mutate_fn(tasks, corpus)
    return "\n".join(validate.check_corpus(tasks, corpus))


def test_corpus_gate_failures(fixtures_dir, tmp_path):
    def stray(tasks, corpus):
        (tasks / "not-in-corpus").mkdir()
    assert "not-in-corpus: not in corpus.json" in problems_for(fixtures_dir, tmp_path / "a", stray)

    def no_dir(tasks, corpus):
        shutil.rmtree(tasks / "tiny-ts-1")
    assert "tiny-ts-1: no task directory" in problems_for(fixtures_dir, tmp_path / "b", no_dir)

    def no_json(tasks, corpus):
        (tasks / "tiny-ts-1" / "task.json").unlink()
    assert "task.json missing" in problems_for(fixtures_dir, tmp_path / "c", no_json)

    def lang(tasks, corpus):
        c = json.loads(corpus.read_text())
        c["tasks"][0]["language"] = "typescript"
        corpus.write_text(json.dumps(c))
    out = problems_for(fixtures_dir, tmp_path / "d", lang)
    assert "language is 'python' in task.json but 'typescript' in corpus.json" in out

    def one_lang(tasks, corpus):
        c = json.loads(corpus.read_text())
        c["tasks"] = c["tasks"][:1]
        corpus.write_text(json.dumps(c))
        shutil.rmtree(tasks / "tiny-ts-1")
    assert "no typescript task" in problems_for(fixtures_dir, tmp_path / "e", one_lang)

    def two_refs(tasks, corpus):
        p, d = sols(tasks)
        d["reference-2"] = dict(d["reference"])
        shutil.copy(p.parent / "reference.patch", p.parent / "reference-2.patch")
        p.write_text(json.dumps(d))
    assert "exactly one reference" in problems_for(fixtures_dir, tmp_path / "f", two_refs)

    def few_flawed(tasks, corpus):
        c = json.loads(corpus.read_text())
        c["min_flawed"] = 3
        corpus.write_text(json.dumps(c))
    assert "at least 3 flawed" in problems_for(fixtures_dir, tmp_path / "g", few_flawed)

    def no_alt(tasks, corpus):
        p, d = sols(tasks)
        del d["alt-1"]
        p.write_text(json.dumps(d))
    assert "at least 1 alt" in problems_for(fixtures_dir, tmp_path / "h", no_alt)

    def no_patch(tasks, corpus):
        (tasks / "tiny-python-1" / "solutions" / "flawed-1.patch").unlink()
    assert "solutions/flawed-1.patch is missing" in problems_for(fixtures_dir, tmp_path / "i", no_patch)

    def no_expect_failing(tasks, corpus):
        p, d = sols(tasks)
        d["flawed-1"]["expect_failing"] = []
        p.write_text(json.dumps(d))
    assert "flawed solution 'flawed-1' has no expect_failing" in \
        problems_for(fixtures_dir, tmp_path / "j", no_expect_failing)

    def no_alt_desc(tasks, corpus):
        p, d = sols(tasks)
        d["alt-1"]["description"] = "  "
        p.write_text(json.dumps(d))
    assert "alt solution 'alt-1' has no description" in \
        problems_for(fixtures_dir, tmp_path / "k", no_alt_desc)

    def no_mutation(tasks, corpus):
        tj = tasks / "tiny-python-1" / "task.json"
        d = json.loads(tj.read_text())
        del d["mutation"]
        tj.write_text(json.dumps(d))
    assert "no mutation config" in problems_for(fixtures_dir, tmp_path / "l", no_mutation)

    for fname in ("expected_tests.json", "TASK.md", "checks.sh", "grader_prompt.md"):
        def drop(tasks, corpus, fname=fname):
            (tasks / "tiny-python-1" / fname).unlink()
        assert f"{fname} is missing" in problems_for(fixtures_dir, tmp_path / f"m-{fname}", drop)


def test_corpus_cli_reports_and_fails(fixtures_dir, tmp_path, capsys):
    tasks, corpus = build_corpus(fixtures_dir, tmp_path)
    (tasks / "tiny-ts-1" / "TASK.md").unlink()
    rc = validate.main(["--tasks-dir", str(tasks), "--corpus", str(corpus), "--check-corpus"])
    assert rc == 1 and "CORPUS GATE FAILED" in capsys.readouterr().err


def test_load_tasks_strict_rejects_stray_dirs(fixtures_dir, tmp_path):
    tasks, _ = build_corpus(fixtures_dir, tmp_path)
    (tasks / "stray").mkdir()
    assert len(load_tasks(tasks)) == 2  # non-production dir: stray is skipped
    with pytest.raises(TaskError, match="stray"):
        load_tasks(tasks, strict=True)


def test_repo_corpus_file_is_well_formed():
    from harness.tasks import load_corpus
    c = load_corpus()
    assert {t["language"] for t in c["tasks"]} == {"python", "typescript"}
    assert c["min_flawed"] == 3 and c["min_alt"] == 1


# --- mutation gate (R08) ------------------------------------------------------------

def mut_task(fixtures_dir, tmp_path, lines=None, targets=None):
    t = load_task(copy_task(fixtures_dir, tmp_path))
    if targets is not None:
        t.data["mutation"] = {"targets": targets}
    elif lines is not None:
        t.data["mutation"] = {"targets": [{"file": "tiny/mathx.py", "lines": lines}]}
    return t


@pytest.mark.parametrize("lines", [[[0, 3]], [[5, 3]], [[1, 999]], [[-1, 2]]])
def test_out_of_range_target_is_an_error(fixtures_dir, tmp_path, lines):
    t = mut_task(fixtures_dir, tmp_path, lines)
    with pytest.raises(mutate.MutationError, match="line range"):
        mutate.list_mutants(t)


def test_range_without_mutants_is_an_error(fixtures_dir, tmp_path):
    t = mut_task(fixtures_dir, tmp_path, [[1, 1], [3, 5]])  # line 1 is a bare def line
    with pytest.raises(mutate.MutationError, match="generate no mutants"):
        mutate.list_mutants(t)


def test_too_few_mutants_is_an_error(fixtures_dir, tmp_path):
    t = mut_task(fixtures_dir, tmp_path, [[3, 3]])
    with pytest.raises(mutate.MutationError, match="need at least 5"):
        mutate.list_mutants(t)


def test_missing_mutation_config_is_an_error(fixtures_dir, tmp_path):
    t = mut_task(fixtures_dir, tmp_path, targets=[])
    with pytest.raises(mutate.MutationError, match="no mutation targets"):
        mutate.list_mutants(t)


def test_production_set_requires_a_config_for_every_task(monkeypatch, fixtures_dir, tmp_path):
    tasks, corpus = build_corpus(fixtures_dir, tmp_path)
    tj = tasks / "tiny-ts-1" / "task.json"
    d = json.loads(tj.read_text())
    del d["mutation"]
    tj.write_text(json.dumps(d))
    monkeypatch.setattr(mutate, "is_production_dir", lambda p: True)
    monkeypatch.setattr(mutate, "load_corpus", lambda: json.loads(corpus.read_text()))
    with pytest.raises(mutate.MutationError, match="tiny-ts-1: no mutation config"):
        mutate.select_tasks(tasks, None)
    # fixture dirs (not production) just skip a task without a config
    monkeypatch.setattr(mutate, "is_production_dir", lambda p: False)
    assert [t.id for t in mutate.select_tasks(tasks, None)] == ["tiny-python-1"]


def error_grade():
    return {"verdict": "ERROR", "tests": {}, "checks": {},
            "runner": {"exit": 2, "timed_out": False, "junit_ok": False, "diag_tail": "boom"}}


def test_mutant_error_fails_the_job_and_is_not_a_kill(task, monkeypatch):
    monkeypatch.setattr(mutate, "baseline_passes", lambda t: True)
    monkeypatch.setattr(mutate, "run_mutant", lambda t, m, keep=False: error_grade())
    with pytest.raises(mutate.MutationInfraError, match="ERROR"):
        mutate.mutate_task(task)


def test_only_fail_kills_a_mutant(task, monkeypatch):
    monkeypatch.setattr(mutate, "baseline_passes", lambda t: True)
    verdicts = iter(["FAIL", "PASS", "FAIL", "PASS", "FAIL", "PASS", "FAIL"])
    monkeypatch.setattr(mutate, "run_mutant", lambda t, m, keep=False: {
        "verdict": next(verdicts), "runner": {}})
    t = load_task(task.path)
    # serial pool order is stable enough: use a single worker
    s = mutate.mutate_task(t, workers=1)
    assert s["mutants"] == 7 and s["killed"] == 4


def test_baseline_must_pass_before_mutants_run(task, monkeypatch):
    called = []
    monkeypatch.setattr(mutate, "baseline_passes", lambda t: False)
    monkeypatch.setattr(mutate, "run_mutant", lambda *a, **k: called.append(1))
    with pytest.raises(mutate.MutationError, match="do not PASS"):
        mutate.mutate_task(task)
    assert called == []


@pytest.mark.parametrize("value", ["", "   ", "too short", 5, None, ["a long enough justification"]])
def test_blank_or_short_justification_is_unjustified(task, value):
    t = load_task(task.path)
    mutants = mutate.list_mutants(t)
    killed = {m["id"]: True for m in mutants}
    killed["tiny/mathx.py:3:lt-le:1"] = False
    t.equivalent_mutants = lambda: {"tiny/mathx.py:3:lt-le:1": value}
    s = mutate.evaluate(t, mutants, killed)
    assert [m["id"] for m in s["unjustified"]] == ["tiny/mathx.py:3:lt-le:1"]
    assert s["unjustified_entries"] == ["tiny/mathx.py:3:lt-le:1"]


def test_good_justification_counts(task):
    t = load_task(task.path)
    mutants = mutate.list_mutants(t)
    killed = {m["id"]: True for m in mutants}
    killed["tiny/mathx.py:3:lt-le:1"] = False
    t.equivalent_mutants = lambda: {"tiny/mathx.py:3:lt-le:1": "x" * 10 + " " * 5 + "y" * 10}
    s = mutate.evaluate(t, mutants, killed)
    assert s["unjustified"] == [] and s["equivalent"] == 1


# --- upstream provenance (R03) --------------------------------------------------------

GIT_PATCH = ("diff --git a/pkg/a.py b/pkg/a.py\nindex 1..2 100644\n--- a/pkg/a.py\n+++ b/pkg/a.py\n"
             "@@ -1,3 +1,4 @@\n one\n-two\n+TWO\n+two-b\n three\n"
             "diff --git a/pkg/b.py b/pkg/b.py\n--- a/pkg/b.py\n+++ b/pkg/b.py\n"
             "@@ -1 +1 @@\n-x\n+y\n")
PLAIN_PATCH = ("--- pkg/a.py\t2020-01-01\n+++ pkg/a.py\t2020-01-02\n"
               "@@ -1,3 +1,4 @@\n one\n-two\n+TWO\n+two-b\n three\n"
               "--- pkg/b.py\n+++ pkg/b.py\n@@ -1 +1 @@\n-x\n+y\n")
PREFIXED_PLAIN = PLAIN_PATCH.replace("--- pkg", "--- a/pkg").replace("+++ pkg", "+++ b/pkg")
EXPECTED_RUNS = {"pkg/a.py": [["-two", "+TWO", "+two-b"]], "pkg/b.py": [["-x", "+y"]]}


@pytest.mark.parametrize("patch", [GIT_PATCH, PLAIN_PATCH, PREFIXED_PLAIN])
def test_parse_both_diff_formats(patch):
    assert verify_upstream.parse_patch_runs(patch) == EXPECTED_RUNS


def test_parse_handles_removed_line_that_looks_like_a_header():
    patch = "--- a/f.py\n+++ b/f.py\n@@ -1,2 +1,1 @@\n--- not a header\n keep\n"
    assert verify_upstream.parse_patch_runs(patch) == {"f.py": [["--- not a header"]]}


def test_parse_rejects_empty_parse():
    for text in ("", "\n\nthis is not a patch\n", "index 123..456\n"):
        with pytest.raises(verify_upstream.PatchParseError):
            verify_upstream.parse_patch_runs(text)


def test_parse_rejects_fewer_files_than_plus_headers():
    patch = ("--- a/f.py\n+++ b/f.py\n@@ -1 +1 @@\n-a\n+b\n"
             "--- a/f.py\n+++ b/f.py\n@@ -5 +5 @@\n-c\n+d\n")
    with pytest.raises(verify_upstream.PatchParseError, match="'\\+\\+\\+' headers"):
        verify_upstream.parse_patch_runs(patch)


def test_missing_hunks_uses_strict_parser():
    with pytest.raises(verify_upstream.PatchParseError):
        verify_upstream.missing_hunks("", {})


PR_OK = {"merged": True, "merge_commit_sha": "1111111111111111111111111111111111111111"}


def fake_gh(pr, commit):
    def gh(endpoint):
        return pr if "/pulls/" in endpoint else commit
    return gh


def good_commit(task):
    return {"parents": [{"sha": task.upstream["base_sha"]}]}


def test_provenance_ok(task):
    problems, notes = verify_upstream.check_provenance(task, fake_gh(PR_OK, good_commit(task)))
    assert problems == [] and notes == []


def test_provenance_unmerged(task):
    pr = dict(PR_OK, merged=False)
    problems, _ = verify_upstream.check_provenance(task, fake_gh(pr, good_commit(task)))
    assert any("not merged" in p for p in problems)


def test_provenance_base_not_a_parent_needs_a_note(task):
    t = load_task(task.path)
    commit = {"parents": [{"sha": "f" * 40}]}
    problems, _ = verify_upstream.check_provenance(t, fake_gh(PR_OK, commit))
    assert any("not a parent" in p and "base_note" in p for p in problems)
    t.data["base_note"] = "short"
    assert verify_upstream.check_provenance(t, fake_gh(PR_OK, commit))[0] != []
    t.data["base_note"] = "squash merge; the PR branched from this commit, main moved on"
    problems, notes = verify_upstream.check_provenance(t, fake_gh(PR_OK, commit))
    assert problems == [] and any("base_note" in n for n in notes)


def test_provenance_merge_sha_mismatch_needs_a_note(task):
    t = load_task(task.path)
    pr = dict(PR_OK, merge_commit_sha="2" * 40)
    problems, _ = verify_upstream.check_provenance(t, fake_gh(pr, good_commit(t)))
    assert any("merge_note" in p for p in problems)
    t.data["merge_note"] = "merge_sha is the rebased commit on main, PR sha is pre-rebase"
    assert verify_upstream.check_provenance(t, fake_gh(pr, good_commit(t)))[0] == []


def patched_bytes(task):
    ws = make_workspace(task, task.patch_path("reference"))
    try:
        return (ws / "tiny" / "mathx.py").read_bytes()
    finally:
        remove_tree(ws)


def test_reference_bytes_match(task):
    want = patched_bytes(task)
    problems, notes = verify_upstream.compare_reference_bytes(task, lambda r, s, p: want)
    assert problems == [] and notes == []


def test_reference_bytes_mismatch_and_deviation(task):
    t = load_task(task.path)
    raw = lambda r, s, p: b"something else\n"  # noqa: E731
    problems, _ = verify_upstream.compare_reference_bytes(t, raw)
    assert any("tiny/mathx.py" in p and "differs" in p for p in problems)
    t.data["reference_deviations"] = {"tiny/mathx.py": "short"}
    assert verify_upstream.compare_reference_bytes(t, raw)[0] != []
    t.data["reference_deviations"] = {"tiny/mathx.py": "upstream also reformatted the whole file"}
    problems, notes = verify_upstream.compare_reference_bytes(t, raw)
    assert problems == [] and len(notes) == 1


def test_stale_and_untouched_deviations_are_flagged(task):
    t = load_task(task.path)
    want = patched_bytes(t)
    t.data["reference_deviations"] = {"tiny/mathx.py": "upstream also reformatted the whole file",
                                      "tiny/other.py": "this file is not touched by the patch at all"}
    problems, _ = verify_upstream.compare_reference_bytes(t, lambda r, s, p: want)
    assert any("stale entry" in p for p in problems)
    assert any("tiny/other.py" in p and "not touched" in p for p in problems)


def test_reference_bytes_fetch_failure_is_a_problem(task):
    def raw(r, s, p):
        raise urllib.error.HTTPError("u", 500, "boom", {}, None)
    problems, _ = verify_upstream.compare_reference_bytes(task, raw)
    assert any("could not fetch" in p for p in problems)


# --- R16: boltons dependency check ---------------------------------------------------

def boltons_checks(tmp_path, helper_src, dictutils_extra="from ._helper import value\n"):
    t = load_task(Path(__file__).resolve().parent.parent / "tasks" / "boltons-458")
    ws = make_workspace(t, None, root=tmp_path / "ws")
    try:
        (ws / "boltons" / "_helper.py").write_text(helper_src, encoding="utf-8")
        p = ws / "boltons" / "dictutils.py"
        p.write_bytes(p.read_bytes() + dictutils_extra.encode())
        return grade.run_checks(t, ws)
    finally:
        remove_tree(ws)


def test_new_internal_helper_is_not_a_new_dependency(tmp_path):
    r = boltons_checks(tmp_path, "import os\nvalue = os.sep\n")
    assert "check:no-new-deps" not in r["failed"] and "check:scope" not in r["failed"]


def test_helper_importing_a_third_party_package_fails(tmp_path):
    r = boltons_checks(tmp_path, "import requests\nvalue = 1\n")
    assert "check:no-new-deps" in r["failed"]


def test_unresolved_relative_import_fails(tmp_path):
    r = boltons_checks(tmp_path, "value = 1\n", dictutils_extra="from ._missing import value\n")
    assert "check:no-new-deps" in r["failed"]
