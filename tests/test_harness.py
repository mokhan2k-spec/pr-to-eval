"""Unit tests for the harness. Fast, no network, no agents."""
import json
import sys

import pytest

from harness import (agent_runs, grade, llm_grade, mutate, readme, report,
                     validate, verify_upstream)
from harness.tasks import TaskError, load_task, load_tasks, validate_solutions_data
from harness.workspace import ApplyError, make_workspace, remove_tree


# --- tasks -----------------------------------------------------------------

def test_load_tasks_and_sorting(fixtures_dir):
    tasks = load_tasks(fixtures_dir)
    assert [t.id for t in tasks] == ["tiny-python-1"]
    names = [n for n, _ in tasks[0].sorted_solutions()]
    assert names == ["reference", "alt-1", "flawed-1", "flawed-2"]


def test_unknown_task_filter(fixtures_dir):
    with pytest.raises(TaskError):
        load_tasks(fixtures_dir, only="nope")


def test_solutions_validation_rules():
    bad = {"reference": {"kind": "reference", "expect": "pass"},
           "flawed-1": {"kind": "flawed", "expect": "fail"}}
    assert any("expect_failing" in p for p in validate_solutions_data(bad))
    assert validate_solutions_data({"alt-1": {}}) != []


def test_id_must_match_directory(tmp_path, fixtures_dir):
    import shutil
    dst = tmp_path / "other-name"
    shutil.copytree(fixtures_dir / "tiny-python-1", dst)
    with pytest.raises(TaskError):
        load_task(dst)


# --- workspace -------------------------------------------------------------

def test_workspace_applies_patch(task, tmp_path):
    ws = make_workspace(task, task.patch_path("reference"), root=tmp_path)
    try:
        assert "value > high" in (ws / "tiny" / "mathx.py").read_text()
        # pristine upstream is untouched
        assert "value > high" not in (task.upstream_dir / "tiny" / "mathx.py").read_text()
    finally:
        remove_tree(ws)


def test_failed_apply_raises_and_cleans_up(task, tmp_path):
    bad = tmp_path / "bad.patch"
    bad.write_text("diff --git a/nope.py b/nope.py\n--- a/nope.py\n+++ b/nope.py\n@@ -1 +1 @@\n-x\n+y\n")
    root = tmp_path / "ws"
    with pytest.raises(ApplyError):
        make_workspace(task, bad, root=root)
    assert list(root.iterdir()) == []


# --- grade -----------------------------------------------------------------

def test_parse_junit(tmp_path):
    xml = tmp_path / "j.xml"
    xml.write_text('<testsuites><testsuite>'
                   '<testcase classname="a.b" name="ok"/>'
                   '<testcase classname="a.b" name="bad"><failure/></testcase>'
                   '<testcase classname="a.b" name="err"><error/></testcase>'
                   '<testcase classname="a.b" name="skip"><skipped/></testcase>'
                   '</testsuite></testsuites>')
    r = grade.parse_junit(xml)
    assert r == {"ok": True, "total": 4, "passed": 1, "failed": ["a.b::bad", "a.b::err"],
                 "skipped": ["a.b::skip"], "ids": ["a.b::ok", "a.b::bad", "a.b::err", "a.b::skip"]}


def test_parse_junit_missing_file(tmp_path):
    r = grade.parse_junit(tmp_path / "none.xml")
    assert r["total"] == 0 and r["ok"] is False


def test_resolve_cmd_python_and_junit(tmp_path):
    cmd = grade.resolve_cmd(["python", "-m", "pytest", "--junitxml={junit}"], tmp_path / "j.xml")
    assert cmd[0] == sys.executable
    assert cmd[-1].endswith("j.xml") and "{junit}" not in cmd[-1]


def test_grade_reference_alt_flawed(task):
    ref = grade.grade_patch(task, task.patch_path("reference"))
    assert ref["verdict"] == "PASS"
    assert ref["tests"] == {"total": 3, "passed": 3, "failed": [], "skipped": [], "missing": []}
    assert ref["checks"] == {"total": 1, "passed": 1, "failed": []}
    assert grade.grade_patch(task, task.patch_path("alt-1"))["verdict"] == "PASS"
    f1 = grade.grade_patch(task, task.patch_path("flawed-1"))
    assert f1["verdict"] == "FAIL"
    assert f1["tests"]["failed"] == ["hidden_tests.test_hidden::test_clamp_above"]
    f2 = grade.grade_patch(task, task.patch_path("flawed-2"))
    assert f2["verdict"] == "FAIL" and f2["checks"]["failed"] == ["check:visible-tests-untouched"]


def test_grade_bad_patch_is_fail_with_apply_error(task, tmp_path):
    bad = tmp_path / "bad.patch"
    bad.write_text("this is not a patch\n")
    r = grade.grade_patch(task, bad)
    assert r["verdict"] == "FAIL" and r["apply_error"]


def test_grade_empty_patch_fails(task, tmp_path):
    empty = tmp_path / "empty.patch"
    empty.write_text("")
    r = grade.grade_patch(task, empty)
    assert r["verdict"] == "FAIL"  # nothing to grade: FAIL without running anything
    assert r["tests"]["failed"] == ["<empty-patch>"]
    assert "apply_error" not in r


def test_timeout_counts_as_failure(task, monkeypatch):
    t = load_task(task.path)
    t.data["test_cmd"] = ["python", "-c", "import time; time.sleep(30)"]
    t.data["timeout_s"] = 1
    r = grade.grade_patch(t, t.patch_path("reference"))
    assert r["runner"]["timed_out"] and r["verdict"] == "FAIL"
    assert r["tests"]["failed"] == ["<timeout>"]


# --- validate --------------------------------------------------------------

def test_matches_rules():
    res = {"verdict": "FAIL", "tests": {"failed": ["x::a"]}, "checks": {"failed": ["check:c"]}}
    assert validate.matches({"expect": "fail", "expect_failing": ["x::a"]}, res)
    assert validate.matches({"expect": "fail", "expect_failing": ["x::a", "check:c"]}, res)
    assert not validate.matches({"expect": "fail", "expect_failing": ["x::other"]}, res)
    assert not validate.matches({"expect": "pass"}, res)
    ok = {"verdict": "PASS", "tests": {"failed": []}, "checks": {"failed": []}}
    assert validate.matches({"expect": "pass"}, ok)
    assert not validate.matches({"expect": "fail", "expect_failing": ["x"]}, ok)


def test_validate_table_is_deterministic_and_matches(task, fixtures_dir):
    tasks = load_tasks(fixtures_dir)
    t1 = validate.render_table(validate.collect_rows(tasks))
    t2 = validate.render_table(validate.collect_rows(tasks))
    assert t1 == t2
    assert t1.endswith("**4/4 solutions match their expected verdict.**")
    assert "| flawed-1 | flawed | 2/3 | 1/1 | — | FAIL | FAIL | yes |" in t1


def test_validate_cli_readme_roundtrip(fixtures_dir, tmp_path, capsys):
    rd = tmp_path / "README.md"
    rd.write_text("x\n<!-- validation-table:start -->\n<!-- validation-table:end -->\ny\n")
    base = ["--tasks-dir", str(fixtures_dir), "--readme", str(rd)]
    assert validate.main(base + ["--check-readme"]) == 1  # empty block is stale
    assert validate.main(base + ["--write-readme"]) == 0
    assert validate.main(base + ["--check-readme"]) == 0
    assert "tiny-python-1" in rd.read_text()


def test_validate_exit_1_on_mismatch(fixtures_dir, tmp_path):
    import shutil
    d = tmp_path / "tasks"
    shutil.copytree(fixtures_dir / "tiny-python-1", d / "tiny-python-1")
    sol = d / "tiny-python-1" / "solutions" / "solutions.json"
    data = json.loads(sol.read_text())
    data["flawed-1"]["expect_failing"] = ["hidden_tests.test_hidden::test_clamp_below"]
    sol.write_text(json.dumps(data))
    assert validate.main(["--tasks-dir", str(d)]) == 1


# --- readme ----------------------------------------------------------------

def test_readme_replace_and_check(tmp_path):
    p = tmp_path / "R.md"
    p.write_text("a\n<!-- m:start -->\nold\n<!-- m:end -->\nb\n")
    assert not readme.check_block("m", "new", p)
    readme.write_block("m", "new\n", p)
    assert p.read_text() == "a\n<!-- m:start -->\nnew\n<!-- m:end -->\nb\n"
    assert readme.check_block("m", "new", p)
    with pytest.raises(readme.ReadmeError):
        readme.write_block("absent", "x", p)


# --- mutate ----------------------------------------------------------------

def mutants_of(line, lang="python", depth=0):
    mask = mutate.code_mask(line, lang)[0]
    return {(op, occ): out for op, occ, out in mutate.line_mutants(line, mask, depth, lang)}


def test_operators_python():
    m = mutants_of("    if a == b and not c:")
    assert m[("eq-ne", 1)] == "    if a != b and not c:"
    assert m[("and-or", 1)] == "    if a == b or not c:"
    assert m[("not-remove", 1)] == "    if a == b and c:"
    assert mutants_of("x = True")[("true-false", 1)] == "x = False"
    assert mutants_of("n = k + 1")[("plus1-minus1", 1)] == "n = k - 1"
    assert mutants_of("if a < b:")[("lt-le", 1)] == "if a <= b:"
    assert mutants_of("if a >= b:")[("ge-gt", 1)] == "if a > b:"


def test_strings_and_comments_are_skipped():
    assert set(op for op, _ in mutants_of('msg = "a == b and True"')) <= {"delete-stmt"}
    assert mutants_of("# a == b") == {}
    assert ("eq-ne", 1) in mutants_of("x = a == b  # not c")
    assert ("not-remove", 1) not in mutants_of("x = a == b  # not c")


def test_return_and_delete_python():
    assert mutants_of("    return a + b")[("return-none", 1)] == "    return None"
    assert ("return-none", 1) not in mutants_of("    return None")
    assert mutants_of("    foo(x)")[("delete-stmt", 1)] == "    pass"
    assert mutants_of("    self.x = 3")[("delete-stmt", 1)] == "    pass"
    assert ("delete-stmt", 1) not in mutants_of("    if x == 1:")
    assert ("delete-stmt", 1) not in mutants_of("    foo(x,")
    # inside an open bracket: not a statement
    assert ("delete-stmt", 1) not in mutants_of("    foo(x)", depth=1)


def test_operators_typescript():
    m = mutants_of("  if (a === b && ok) {", "typescript")
    assert m[("eq-ne", 1)] == "  if (a !== b && ok) {"
    assert m[("andand-oror", 1)] == "  if (a === b || ok) {"
    assert mutants_of("  return x + 1;", "typescript")[("return-none", 1)] == "  return undefined;"
    assert mutants_of("  log(x);", "typescript")[("delete-stmt", 1)] == ""
    assert mutants_of("  const q = 1;", "typescript").get(("delete-stmt", 1)) is None
    assert mutants_of("  s = `a === b`;", "typescript").get(("eq-ne", 1)) is None


def test_negate_condition():
    assert mutants_of("    if x == 1:")[("negate-cond", 1)] == "    if not (x == 1):"
    assert mutants_of("    while n and ok:")[("negate-cond", 1)] == "    while not (n and ok):"
    ts = mutants_of("  if (signal?.aborted) {", "typescript")
    assert ts[("negate-cond", 1)] == "  if (!(signal?.aborted)) {"
    assert mutants_of("  } else if (a) {", "typescript")[("negate-cond", 1)] == "  } else if (!(a)) {"
    # multi-line conditions and comments are left alone
    assert ("negate-cond", 1) not in mutants_of("    if (a and")
    assert mutants_of("# if x:") == {}


def test_python_docstring_lines_masked():
    text = 'def f():\n    """a == b\n    and True"""\n    return 1\n'
    mask = mutate.code_mask(text, "python")
    lines = text.split("\n")
    assert list(mutate.line_mutants(lines[1], mask[1], 0, "python")) == []
    assert list(mutate.line_mutants(lines[2], mask[2], 0, "python")) == []


def test_generate_ids_are_stable(task):
    a = [m["id"] for m in mutate.list_mutants(task)]
    b = [m["id"] for m in mutate.list_mutants(task)]
    assert a == b
    assert "tiny/mathx.py:3:lt-le:1" in a and "tiny/mathx.py:5:gt-ge:1" in a


def test_mutate_end_to_end(task):
    s = mutate.mutate_task(task)
    assert (s["mutants"], s["killed"], s["equivalent"]) == (7, 5, 2)  # incl. 2 negate-cond
    assert s["unjustified"] == [] and s["stale"] == []
    assert s["score"] == "100.0%"


def test_mutate_detects_unjustified_and_stale(task):
    t = load_task(task.path)
    mutants = mutate.list_mutants(t)
    killed = {m["id"]: True for m in mutants}
    killed["tiny/mathx.py:5:gt-ge:1"] = False  # survives; 3:lt-le:1 is listed but "killed"
    s = mutate.evaluate(t, mutants, killed)
    assert s["stale"] == ["tiny/mathx.py:3:lt-le:1"]
    assert s["unjustified"] == []
    killed["tiny/mathx.py:4:return-none:1"] = False
    s = mutate.evaluate(t, mutants, killed)
    assert [m["id"] for m in s["unjustified"]] == ["tiny/mathx.py:4:return-none:1"]


def test_mutate_cli_list(fixtures_dir, capsys):
    assert mutate.main(["--tasks-dir", str(fixtures_dir), "--list"]) == 0
    assert "lt-le" in capsys.readouterr().out


# --- agent runs / report / llm / upstream ----------------------------------

def test_codex_command_flags(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_runs.sys, "platform", "win32")
    win = agent_runs.build_command("codex", tmp_path)
    assert 'windows.sandbox="elevated"' in win and "project_doc_max_bytes=0" in win
    monkeypatch.setattr(agent_runs.sys, "platform", "linux")
    assert 'windows.sandbox="elevated"' not in agent_runs.build_command("codex", tmp_path)


def test_claude_command_and_env(tmp_path):
    cmd = agent_runs.build_command("claude", tmp_path)
    assert cmd[:2] == ["claude", "-p"] and "--strict-mcp-config" in cmd
    assert agent_runs.agent_env("claude")["CLAUDE_CODE_DISABLE_CLAUDE_MDS"] == "1"


def test_dry_run_runs_nothing(fixtures_dir, capsys, tmp_path):
    rc = agent_runs.main(["--tasks-dir", str(fixtures_dir), "--task", "tiny-python-1",
                          "--agent", "all", "--n", "1", "--dry-run"])
    out = capsys.readouterr().out
    assert rc == 0 and "claude-1" in out and "codex-1" in out


def test_classifier():
    fail = {"verdict": "FAIL", "tests": {"failed": ["t::a"]}, "checks": {"failed": []}}
    c = agent_runs.classify(fail, b"diff", {"exit_code": 0})
    assert c["failure_source"] == "agent" and c["classified_by"] == "auto"
    c = agent_runs.classify(fail, b"", {"exit_code": 1})
    assert c["failure_source"] == "environment"
    c = agent_runs.classify(dict(fail, apply_error="boom"), b"diff", {"exit_code": 0})
    assert c["failure_source"] == "environment"
    ok = {"verdict": "PASS", "tests": {"failed": []}, "checks": {"failed": []}}
    c = agent_runs.classify(ok, b"diff", {})
    assert c["outcome"] == "pass" and c["failure_source"] is None


def test_report_summary(tmp_path):
    for name, verdict, src in [("claude-1", "PASS", None), ("claude-2", "FAIL", "agent")]:
        d = tmp_path / "t1" / name
        d.mkdir(parents=True)
        (d / "grade.json").write_text(json.dumps({"verdict": verdict}))
        (d / "meta.json").write_text(json.dumps({"agent": "claude"}))
        (d / "classification.json").write_text(json.dumps(
            {"outcome": verdict.lower(), "failure_source": src, "evidence": "e", "classified_by": "auto"}))
    out = report.write_summary(tmp_path).read_text()
    assert "| t1 | claude | 1/2 | 1 | 0 | 0 |" in out
    assert "t1/claude-2" in out


def test_llm_extract_json_and_prompt():
    text = 'Sure! ```json\n{"verdict": "PASS", "correctness": 3, "scope": 2, "reasons": "ok {x}"}\n``` done'
    assert llm_grade.extract_json(text)["verdict"] == "PASS"
    assert llm_grade.extract_json("no json {here}") is None
    assert llm_grade.fill_prompt("T={{TASK}} D={{DIFF}}", "a", "b") == "T=a D=b"


def test_llm_dry_run(fixtures_dir, capsys):
    assert llm_grade.main(["--tasks-dir", str(fixtures_dir), "--dry-run"]) == 0
    assert "[dry-run] tiny-python-1/reference" in capsys.readouterr().out


REF = ("diff --git a/f.py b/f.py\n--- a/f.py\n+++ b/f.py\n@@ -3,2 +3,4 @@ def f():\n"
       " a\n+b\n+c\n d\n")


def test_verify_hunk_comparison():
    gh = "@@ -1,6 +1,8 @@\n x\n y\n a\n+b\n+c\n d\n-old\n+new\n z"
    files = {"f.py": verify_upstream.runs_of_github_patch(gh)}
    assert verify_upstream.missing_hunks(REF, files) == []
    other = {"f.py": verify_upstream.runs_of_github_patch("@@ -1 +1 @@\n-q\n+r")}
    assert verify_upstream.missing_hunks(REF, other) != []
    assert verify_upstream.missing_hunks(REF, {}) != []
