"""agent_runs with fake agent executables (tests/fake_agent.py). No real CLI is called."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from harness import agent_runs
from harness.tasks import load_task

from conftest import good_probe_record

FAKE = str(Path(__file__).parent / "fake_agent.py")


def fake(mode):
    return [sys.executable, FAKE, "--fake-mode", mode]


def stub_grade(task, patch, **_kw):
    empty = not Path(patch).read_bytes().strip()
    return {"verdict": "FAIL" if empty else "PASS",
            "tests": {"total": 1, "passed": 0 if empty else 1, "failed": [], "skipped": [], "missing": []},
            "checks": {"total": 0, "passed": 0, "failed": []},
            "runner": {"exit": 0, "timed_out": False, "junit_ok": True, "diag_tail": ""}}


@pytest.fixture
def env(tmp_path, monkeypatch, fixtures_dir):
    monkeypatch.setattr(agent_runs, "runs_root", lambda: tmp_path / "ws")
    monkeypatch.setattr(agent_runs, "grade_patch", stub_grade)
    return {"runs": tmp_path / "runs", "task": load_task(fixtures_dir / "tiny-python-1"),
            "tasks_dir": fixtures_dir, "tmp": tmp_path}


def install_probe(env, agents=("claude", "codex"), cli="fake-cli 1.0", mutate=None):
    """Write runs/_boundary/probe.json with good records bound to the fake CLI."""
    recs = {a: good_probe_record(a, cli) for a in agents}
    if mutate:
        mutate(recs)
    d = env["runs"] / "_boundary"
    d.mkdir(parents=True, exist_ok=True)
    (d / "probe.json").write_text(json.dumps({"agents": recs}), encoding="utf-8")


def fake_launcher(env):
    launcher = env["tmp"] / ("fake.cmd" if os.name == "nt" else "fake.sh")
    if os.name == "nt":
        launcher.write_text(f'@"{sys.executable}" "{FAKE}" %*\r\n')
    else:
        launcher.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE}" "$@"\n')
        launcher.chmod(0o755)
    return launcher


def run(env, mode, agent="claude", trial=1, attempt=1, **kw):
    return agent_runs.run_attempt(env["task"], agent, trial, attempt, env["runs"],
                                  timeout=kw.pop("timeout", 60), exe=fake(mode), **kw)


# --- command lines and environment ---------------------------------------------------

def test_claude_argv_is_path_scoped_and_noninteractive(tmp_path):
    cmd = agent_runs.build_command("claude", tmp_path, ["claude"])
    assert cmd[:2] == ["claude", "-p"]
    joined = " ".join(cmd)
    for part in ("--setting-sources project", "--strict-mcp-config", "--no-session-persistence",
                 "--permission-mode dontAsk", "--output-format stream-json --verbose",
                 "--disallowedTools WebFetch WebSearch Agent"):
        assert part in joined
    allowed = cmd[cmd.index("--allowedTools") + 1: cmd.index("--disallowedTools")]
    assert allowed[:3] == ["Read(./**)", "Edit(./**)", "Write(./**)"]
    assert "Read" not in allowed and "Bash" not in allowed
    assert "Bash(python -m pytest:*)" in allowed and "bypassPermissions" not in joined


def test_claude_absolute_scope_uses_msys_style_root(tmp_path):
    allowed = agent_runs.claude_allowed_tools(tmp_path, "absolute")
    assert allowed[0].startswith("Read(//") and allowed[0].endswith("/**)")


def test_codex_argv(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_runs.sys, "platform", "win32")
    win = agent_runs.build_command("codex", tmp_path, ["codex"])
    assert 'windows.sandbox="elevated"' in win and "project_doc_max_bytes=0" in win
    assert "sandbox_workspace_write.network_access=false" in win
    assert "--ephemeral" in win and "--json" in win and win[win.index("-C") + 1] == str(tmp_path)
    monkeypatch.setattr(agent_runs.sys, "platform", "linux")
    assert 'windows.sandbox="elevated"' not in agent_runs.build_command("codex", tmp_path, ["codex"])


def test_env_is_an_allowlist(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("GITHUB_TOKEN", "x")
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    for agent in agent_runs.AGENTS:
        e = agent_runs.agent_env(agent)
        assert not {"ANTHROPIC_API_KEY", "GITHUB_TOKEN", "OPENAI_API_KEY"} & set(e)
        assert all(k.upper() in agent_runs.ENV_ALLOW or k == "CLAUDE_CODE_DISABLE_CLAUDE_MDS" for k in e)
    assert agent_runs.agent_env("claude")["CLAUDE_CODE_DISABLE_CLAUDE_MDS"] == "1"


def test_dry_run_runs_nothing(fixtures_dir, capsys, tmp_path):
    rc = agent_runs.main(["--tasks-dir", str(fixtures_dir), "--runs-dir", str(tmp_path / "r"),
                          "--task", "tiny-python-1", "--trials", "1", "--dry-run"])
    out = capsys.readouterr().out
    assert rc == 0 and "tiny-python-1/claude/trial-1/attempt-1" in out and "codex/trial-1" in out
    assert not (tmp_path / "r").exists()


def test_parse_trials():
    assert agent_runs.parse_trials("1-5") == [1, 2, 3, 4, 5]
    assert agent_runs.parse_trials("3") == [3] and agent_runs.parse_trials("1,4") == [1, 4]
    with pytest.raises(ValueError):
        agent_runs.parse_trials("0-2")
    with pytest.raises(ValueError):
        agent_runs.parse_trials("6")


# --- patch capture ---------------------------------------------------------

def test_committed_and_uncommitted_changes_are_both_captured(env):
    r = run(env, "commit")
    patch = (r["dir"] / "final.patch").read_text(encoding="utf-8")
    assert "+    if value > high:" in patch            # committed by the agent
    assert "notes.txt" in patch                         # never committed
    assert ".pyc" not in patch                          # ignored
    meta = json.loads((r["dir"] / "meta.json").read_text(encoding="utf-8"))
    assert len(meta["baseline_sha"]) == 40
    assert meta["observed_model"] == "fake-model-1" and meta["timed_out"] is False
    assert meta["exit_code"] == 0 and meta["attempt"] == 1 and meta["trial"] == 1
    assert "ANTHROPIC_API_KEY" not in meta["env_keys"] and "PATH" in meta["env_keys"]
    # the patch applies to a fresh copy of upstream/
    ws = env["tmp"] / "fresh"
    import shutil
    shutil.copytree(env["task"].upstream_dir, ws)
    subprocess.run(["git", "init", "-q"], cwd=ws, check=True)
    subprocess.run(["git", "apply", "-p1", str(r["dir"] / "final.patch")], cwd=ws, check=True)
    assert "return high" in (ws / "tiny" / "mathx.py").read_text(encoding="utf-8")


def test_no_change_gives_empty_patch(env):
    r = run(env, "nochange")
    assert (r["dir"] / "final.patch").read_bytes() == b""
    assert r["classification"]["failure_source"] == "agent" and not r["classification"]["confirmed"]


def test_workspace_is_outside_the_repo_and_removed(env):
    r = run(env, "commit")
    assert not any((env["tmp"] / "ws").rglob("tiny")), "workspace should be deleted"
    assert agent_runs.ROOT not in (env["tmp"] / "ws").parents
    assert r["dir"].name == "attempt-1"


def test_published_attempt_has_every_artifact(env):
    r = run(env, "commit")
    names = sorted(p.name for p in r["dir"].iterdir())
    assert names == ["audit.json", "classification.json", "final.patch", "grade.json",
                     "meta.json", "stderr.txt", "transcript.jsonl"]
    assert not list(r["dir"].parent.glob("*.partial"))
    grade = json.loads((r["dir"] / "grade.json").read_text(encoding="utf-8"))
    assert grade["patch_sha256"] == agent_runs.sha256_file(r["dir"] / "final.patch")
    assert grade["task_hash"] == agent_runs.task_hash(env["task"])
    audit = json.loads((r["dir"] / "audit.json").read_text(encoding="utf-8"))
    assert audit["transcript_sha256"] == agent_runs.sha256_file(r["dir"] / "transcript.jsonl")
    assert audit["clean"] is True
    cls = json.loads((r["dir"] / "classification.json").read_text(encoding="utf-8"))
    assert cls["grade_sha256"] == agent_runs.sha256_file(r["dir"] / "grade.json")
    assert cls["outcome"] == "pass" and cls["confirmed"] is True


def test_timeout_kills_the_agent_and_still_grades(env):
    r = run(env, "hang", timeout=3)
    meta = json.loads((r["dir"] / "meta.json").read_text(encoding="utf-8"))
    assert meta["timed_out"] is True
    assert r["classification"]["failure_source"] == "environment"
    assert (r["dir"] / "grade.json").exists()


def test_escape_makes_the_attempt_contaminated(env):
    r = run(env, "escape")
    assert r["classification"]["outcome"] == "contaminated" and not r["classification"]["confirmed"]
    audit = json.loads((r["dir"] / "audit.json").read_text(encoding="utf-8"))
    assert not audit["clean"] and audit["hits"][0]["kind"] == "path"


def test_codex_attempt_records_unknown_model_and_leak(env):
    r = run(env, "commit", agent="codex")
    meta = json.loads((r["dir"] / "meta.json").read_text(encoding="utf-8"))
    assert meta["observed_model"] is None and "observed_model_note" in meta
    assert meta["known_context_leak"] and meta["requested_effort"] == "medium"


# --- immutability, retries ---------------------------------------------------------

def test_attempt_dirs_are_immutable(env):
    run(env, "commit")
    with pytest.raises(FileExistsError):
        run(env, "commit")
    with pytest.raises(SystemExit):                  # there is no --force
        agent_runs.main(["--force"])


def test_plan_attempt_rules(env):
    runs, tid = env["runs"], env["task"].id
    plan = lambda: agent_runs.plan_attempt(runs, tid, "claude", 1)
    assert plan() == (1, "")
    r1 = run(env, "nochange")                       # agent failure, unconfirmed
    assert plan()[0] is None and "not confirmed" in plan()[1]
    agent_runs.confirm_attempt(r1["dir"], "agent", "agent exited cleanly but changed nothing")
    assert plan()[0] is None and "final" in plan()[1]        # agent failures are final
    # a different trial where the failure is environmental
    r = run(env, "hang", trial=2, timeout=2)
    assert agent_runs.plan_attempt(runs, tid, "claude", 2)[0] is None
    agent_runs.confirm_attempt(r["dir"], "environment", "timed out with no patch")
    assert agent_runs.plan_attempt(runs, tid, "claude", 2) == (2, "")
    run(env, "commit", trial=2, attempt=2)
    n, why = agent_runs.plan_attempt(runs, tid, "claude", 2)
    assert n is None                                           # PASS is final


def test_max_two_attempts_per_trial(env):
    runs, tid = env["runs"], env["task"].id
    r1 = run(env, "hang", timeout=2)
    agent_runs.confirm_attempt(r1["dir"], "environment", "timed out")
    r2 = run(env, "hang", attempt=2, timeout=2)
    agent_runs.confirm_attempt(r2["dir"], "contaminated", "second attempt also failed")
    n, why = agent_runs.plan_attempt(runs, tid, "claude", 1)
    assert n is None and "max 2" in why


def test_main_skips_finished_trials_and_runs_the_rest(env, capsys):
    argv = ["--tasks-dir", str(env["tasks_dir"]), "--runs-dir", str(env["runs"]),
            "--task", "tiny-python-1", "--agent", "claude", "--trials", "1-2", "--jobs", "2",
            "--exe", ""]
    # --exe takes a bare path; run the fake through python by wrapping with a launcher script
    launcher = fake_launcher(env)
    install_probe(env)
    argv[-1] = "claude=" + str(launcher)
    assert agent_runs.main(argv) == 0
    out = capsys.readouterr().out
    assert "trial-1/attempt-1" in out and "trial-2/attempt-1" in out
    assert agent_runs.main(argv) == 0
    again = capsys.readouterr().out
    assert again.count("skipped") == 2 and "attempt-1: " not in again


# --- confirm ---------------------------------------------------------------

def test_confirm_requires_evidence_and_a_valid_source(env):
    r = run(env, "nochange")
    with pytest.raises(ValueError):
        agent_runs.confirm_attempt(r["dir"], "agent", "   ")
    assert agent_runs.main(["confirm", str(r["dir"]), "--source", "agent", "--evidence", " "]) == 1
    assert agent_runs.main(["confirm", str(r["dir"]), "--source", "grader",
                            "--evidence", "tests/test_x.py::t failed but the spec allows it"]) == 0
    cls = json.loads((r["dir"] / "classification.json").read_text(encoding="utf-8"))
    assert cls["confirmed"] and cls["confirmed_by"] == "main-seat" and cls["failure_source"] == "grader"
    assert cls["outcome"] == "fail"


def test_confirm_cannot_blame_a_passing_run(env):
    r = run(env, "commit")
    with pytest.raises(ValueError):
        agent_runs.confirm_attempt(r["dir"], "agent", "x")
    cls = agent_runs.confirm_attempt(r["dir"], "contaminated", "read /pull/ page")
    assert cls["outcome"] == "contaminated" and cls["failure_source"] is None


# --- stale hashes / regrade ----------------------------------------------------------

def test_task_hash_changes_with_any_graded_input(env, tmp_path):
    import shutil
    from harness.tasks import load_task
    copy = tmp_path / "tiny-python-1"
    shutil.copytree(env["task"].path, copy)
    t = load_task(copy)
    base = agent_runs.task_hash(t)
    assert base == agent_runs.task_hash(env["task"])
    for rel in ("hidden/test_hidden.py", "checks.sh", "upstream/tiny/mathx.py", "grader_prompt.md",
                "TASK.md", "task.json"):
        p = copy / rel
        old = p.read_bytes()
        p.write_bytes(old + b"\n# x\n")
        assert agent_runs.task_hash(t) != base, rel
        p.write_bytes(old)
    assert agent_runs.task_hash(t) == base
    (copy / "expected_tests.json").write_text("[]", encoding="utf-8")
    assert agent_runs.task_hash(t) != base


def test_regrade_replaces_stale_grade_and_marks_dependents(env, monkeypatch):
    r = run(env, "commit")
    adir, task = r["dir"], env["task"]
    (adir / "llm_grade.json").write_text(json.dumps({"status": "graded"}), encoding="utf-8")
    assert agent_runs.regrade_attempt(task, adir) is False        # fresh: nothing to do
    assert not (adir / "grade-history.jsonl").exists()
    # the task changes -> the stored grade is stale
    monkeypatch.setattr(agent_runs, "task_hash", lambda t: "f" * 64)
    old_grade = json.loads((adir / "grade.json").read_text(encoding="utf-8"))
    assert agent_runs.regrade_attempt(task, adir) is True
    new_grade = json.loads((adir / "grade.json").read_text(encoding="utf-8"))
    assert new_grade["task_hash"] == "f" * 64
    history = (adir / "grade-history.jsonl").read_text(encoding="utf-8").splitlines()
    assert json.loads(history[0])["task_hash"] == old_grade["task_hash"]
    for name in ("classification.json", "llm_grade.json"):
        assert json.loads((adir / name).read_text(encoding="utf-8"))["stale"] is True
    assert agent_runs.regrade_attempt(task, adir) is False


def test_regrade_cli(env, monkeypatch, capsys):
    r = run(env, "commit")
    monkeypatch.setattr(agent_runs, "task_hash", lambda t: "e" * 64)
    assert agent_runs.main(["regrade", "--tasks-dir", str(env["tasks_dir"]),
                            "--runs-dir", str(env["runs"])]) == 0
    assert "1 regraded" in capsys.readouterr().out


# --- redaction -------------------------------------------------------------

def test_redactor_covers_every_path_form(tmp_path, monkeypatch):
    home = tmp_path / "home" / "alice"
    ws = tmp_path / "ws" / "attempt-1-ab12cd34"
    repo = tmp_path / "repo" / "pr-to-eval"
    for p in (home, ws, repo):
        p.mkdir(parents=True)
    red = agent_runs.Redactor(ws=ws, repo=repo, home=home, user="alice")
    w = str(ws)
    forms = [w, w.replace("\\", "/"), w.replace("\\", "\\\\"), w.replace("\\", "\\\\\\\\"),
             w.upper() if os.name == "nt" else w]
    if os.name == "nt":
        forms.append("/" + w[0].lower() + "/" + w[3:].replace("\\", "/"))
    for f in forms:
        out = red(f"opened {f}\\x.py and {f}/y.py")
        assert "<WS>" in out and "ab12cd34" not in out, f
    assert red(str(repo) + "/tasks") == "<REPO>/tasks"
    assert red(str(home) + "/.cache").startswith("<HOME>")
    assert red("user ALICE logged in") == "user <USER> logged in"
    assert red("alicecooper") == "alicecooper"        # whole words only


def test_redactor_handles_json_lines_and_nested(tmp_path):
    ws = tmp_path / "w1"
    ws.mkdir()
    red = agent_runs.Redactor(ws=ws, repo=tmp_path / "r", home=tmp_path / "h", user="alice")
    line = json.dumps({"cwd": str(ws), "cmd": f"cat {ws}\\a.txt"})
    out = red(line)
    assert str(ws).replace("\\", "\\\\") not in out and json.loads(out)["cwd"] == "<WS>"
    assert red.obj({"a": [str(ws), 3, {"b": str(ws)}]}) == {"a": ["<WS>", 3, {"b": "<WS>"}]}


def test_stored_evidence_has_no_machine_paths(env):
    r = run(env, "commit")
    ws_root = str(env["tmp"] / "ws")
    for f in r["dir"].iterdir():
        text = f.read_text(encoding="utf-8", errors="replace")
        assert ws_root not in text and ws_root.replace("\\", "/") not in text, f.name
        assert str(Path.home()) not in text, f.name
    transcript = (r["dir"] / "transcript.jsonl").read_text(encoding="utf-8")
    assert "<WS>" in transcript          # the fake agent prints its cwd in the init event


def test_unredacted_copies_stay_outside_the_repo(env):
    run(env, "commit")
    raws = list((env["tmp"] / "ws").rglob("*.raw"))
    assert raws and all(agent_runs.ROOT not in r.parents for r in raws)


# --- R14: interrupted attempts, R13: digests, R11: review_required -------------------

def _boom(*_a, **_k):
    raise RuntimeError("grader exploded")


def test_exception_after_launch_leaves_an_incomplete_attempt(env, monkeypatch):
    monkeypatch.setattr(agent_runs, "grade_patch", _boom)
    r = run(env, "commit")
    adir = r["dir"]
    assert r["status"] == "incomplete" and "grader exploded" in r["error"]
    meta = json.loads((adir / "meta.json").read_text(encoding="utf-8"))
    assert meta["status"] == "incomplete" and "RuntimeError" in meta["error"]
    assert (adir / "transcript.jsonl").is_file()                 # salvaged
    assert "+    if value > high:" in (adir / "final.patch").read_text(encoding="utf-8")
    assert not (adir / "grade.json").exists()
    # never deleted, counts toward the allowance; a retry needs the reviewer's interrupted.json
    n, why = agent_runs.plan_attempt(env["runs"], env["task"].id, "claude", 1)
    assert n is None and "interrupted.json" in why
    agent_runs.confirm_interrupted(adir, "grader crashed in the harness; transcript and patch kept")
    assert agent_runs.plan_attempt(env["runs"], env["task"].id, "claude", 1) == (2, "")
    monkeypatch.setattr(agent_runs, "grade_patch", stub_grade)
    assert run(env, "commit", attempt=2)["status"] == "complete"
    assert adir.exists()


def test_incomplete_counts_toward_max_attempts(env, monkeypatch):
    monkeypatch.setattr(agent_runs, "grade_patch", _boom)
    for k in (1, 2):
        agent_runs.confirm_interrupted(run(env, "nochange", attempt=k)["dir"], "harness crash")
    n, why = agent_runs.plan_attempt(env["runs"], env["task"].id, "claude", 1)
    assert n is None and "max 2" in why


def test_started_meta_is_written_before_launch(env, monkeypatch):
    seen = {}

    def spy(*a, **k):
        d = env["runs"] / env["task"].id / "claude" / "trial-1" / "attempt-1"
        seen["meta"] = json.loads((d / "meta.json").read_text(encoding="utf-8"))
        return real(*a, **k)

    real = agent_runs.run_process
    monkeypatch.setattr(agent_runs, "run_process", spy)
    run(env, "nochange")
    assert seen["meta"]["status"] == "started"
    done = json.loads((env["runs"] / env["task"].id / "claude" / "trial-1" / "attempt-1"
                       / "meta.json").read_text(encoding="utf-8"))
    assert done["status"] == "complete" and "statement" in done["boundary"]
    assert "NOT prevented" in done["boundary"]["statement"]


def test_batch_exits_nonzero_when_an_attempt_is_incomplete(env, monkeypatch, capsys):
    monkeypatch.setattr(agent_runs, "grade_patch", _boom)
    launcher = fake_launcher(env)
    install_probe(env)
    argv = ["--tasks-dir", str(env["tasks_dir"]), "--runs-dir", str(env["runs"]),
            "--task", "tiny-python-1", "--agent", "claude", "--trials", "1", "--exe",
            "claude=" + str(launcher)]
    assert agent_runs.main(argv) == 1
    assert "INCOMPLETE" in capsys.readouterr().out


def test_grader_digest_tracks_grader_code_and_toolchain(env, monkeypatch):
    task = env["task"]
    base = agent_runs.grader_digest(task)
    assert base == agent_runs.grader_digest(task)
    monkeypatch.setattr(agent_runs, "_TOOLCHAIN", dict(agent_runs.toolchain_versions(), node="v0"))
    assert agent_runs.grader_digest(task) != base


def test_regrade_triggers_when_grader_digest_changes(env, monkeypatch):
    r = run(env, "commit")
    adir, task = r["dir"], env["task"]
    assert json.loads((adir / "grade.json").read_text(encoding="utf-8"))["grader_digest"]
    assert agent_runs.regrade_attempt(task, adir) is False
    monkeypatch.setattr(agent_runs, "grader_digest", lambda t: "d" * 64)
    assert agent_runs.regrade_attempt(task, adir) is True
    assert json.loads((adir / "classification.json").read_text(encoding="utf-8"))["stale"] is True


def test_reaudit_triggers_when_auditor_changes(env, monkeypatch):
    r = run(env, "commit")
    adir, task = r["dir"], env["task"]
    assert agent_runs.reaudit_attempt(task, adir) is False
    from harness import audit
    monkeypatch.setattr(audit, "auditor_digest", lambda ids=None: "a" * 64)
    assert agent_runs.reaudit_attempt(task, adir) is True
    assert json.loads((adir / "audit.json").read_text(encoding="utf-8"))["auditor_digest"] == "a" * 64
    assert (adir / "audit-history.jsonl").is_file()
    assert json.loads((adir / "classification.json").read_text(encoding="utf-8"))["stale"] is True


def test_review_required_blocks_auto_confirm_until_cleared():
    grade = {"verdict": "PASS", "tests": {"failed": []}, "checks": {"failed": []}}
    aud = {"clean": True, "hits": [],
           "review_required": [{"where": "patch a.py:3", "text": "import socket", "pattern": "socket"}]}
    cls = agent_runs.classify(grade, b"x", {}, aud)
    assert cls["outcome"] == "pass" and cls["confirmed"] is False and "import socket" in cls["evidence"]
    cls = agent_runs.classify(grade, b"x", {}, dict(aud, review_required=[]))
    assert cls["confirmed"] is True


def test_confirm_needs_review_clearance(env):
    r = run(env, "commit")
    aud = json.loads((r["dir"] / "audit.json").read_text(encoding="utf-8"))
    aud["review_required"] = [{"where": "patch a.py:3", "text": "import socket", "pattern": "socket"}]
    (r["dir"] / "audit.json").write_text(json.dumps(aud), encoding="utf-8")
    with pytest.raises(ValueError, match="review"):
        agent_runs.confirm_attempt(r["dir"], "pass", "tests passed")
    cls = agent_runs.confirm_attempt(r["dir"], "pass", "tests passed", review_cleared="socket is stdlib import for a test double")
    assert cls["review_cleared"].startswith("socket") and cls["outcome"] == "pass"


# --- R18: the boundary probe gates execution -------------------------------------------

def gate_argv(env, launcher, *extra):
    return ["--tasks-dir", str(env["tasks_dir"]), "--runs-dir", str(env["runs"]),
            "--task", "tiny-python-1", "--agent", "claude", "--trials", "1",
            "--exe", "claude=" + str(launcher), *extra]


def attempt_dirs(env):
    return list((env["runs"] / "tiny-python-1").glob("*/trial-*/attempt-*"))


def test_missing_probe_refuses_the_launch(env, capsys):
    rc = agent_runs.main(gate_argv(env, fake_launcher(env)))
    cap = capsys.readouterr()
    assert rc == 2 and "boundary probe missing" in cap.err
    assert attempt_dirs(env) == []


def test_missing_agent_record_refuses(env, capsys):
    install_probe(env, agents=("codex",))
    assert agent_runs.main(gate_argv(env, fake_launcher(env))) == 2
    assert "no boundary probe record" in capsys.readouterr().err and attempt_dirs(env) == []


def test_failed_probe_refuses(env, capsys):
    def breakit(recs):
        recs["claude"]["net_urllib"] = {"result": "allowed"}
    install_probe(env, mutate=breakit)
    assert agent_runs.main(gate_argv(env, fake_launcher(env))) == 2
    assert "net_urllib is allowed (must be denied)" in capsys.readouterr().err
    assert attempt_dirs(env) == []


def test_probe_without_binding_refuses(env, capsys):
    install_probe(env, mutate=lambda recs: recs["claude"].pop("_binding"))
    assert agent_runs.main(gate_argv(env, fake_launcher(env))) == 2
    assert "no _binding" in capsys.readouterr().err and attempt_dirs(env) == []


@pytest.mark.parametrize("field", ["policy_digest", "probe_digest", "cli_version"])
def test_stale_binding_refuses(env, capsys, field):
    def stale(recs):
        recs["claude"]["_binding"][field] = "stale"
    install_probe(env, mutate=stale)
    assert agent_runs.main(gate_argv(env, fake_launcher(env))) == 2
    assert f"_binding.{field} is stale" in capsys.readouterr().err and attempt_dirs(env) == []


def test_changed_policy_makes_the_probe_stale(env, monkeypatch, capsys):
    install_probe(env)
    monkeypatch.setattr(agent_runs, "CLAUDE_MODEL", "other-model")
    assert agent_runs.main(gate_argv(env, fake_launcher(env))) == 2
    assert "_binding.policy_digest is stale" in capsys.readouterr().err


def test_good_probe_launches_and_embeds_the_record(env, capsys):
    install_probe(env)
    assert agent_runs.main(gate_argv(env, fake_launcher(env))) == 0
    (adir,) = attempt_dirs(env)
    meta = json.loads((adir / "meta.json").read_text(encoding="utf-8"))
    probe = meta["boundary"]["probe"]
    assert probe["_binding"]["policy_digest"] == agent_runs.policy_digest("claude")
    assert probe["_binding"]["cli_version"] == meta["cli_version"] == "fake-cli 1.0"


def test_dry_run_reports_the_gate_but_does_not_fail(env, capsys):
    rc = agent_runs.main(gate_argv(env, fake_launcher(env), "--dry-run"))
    out = capsys.readouterr().out
    assert rc == 0 and "[probe-gate] claude: REFUSED" in out and "probe problem" in out
    assert attempt_dirs(env) == []


def test_policy_and_probe_digests_are_deterministic(tmp_path, monkeypatch):
    for agent in agent_runs.AGENTS:
        a = agent_runs.policy_digest(agent)
        assert a == agent_runs.policy_digest(agent) and len(a) == 64
        monkeypatch.chdir(tmp_path)
        assert agent_runs.policy_digest(agent) == a
    assert agent_runs.policy_digest("claude") != agent_runs.policy_digest("codex")
    assert agent_runs.probe_digest() == agent_runs.sha256_file(
        Path(agent_runs.__file__).parent / "boundary_probe.py")
    monkeypatch.setattr(agent_runs, "READ_SCOPE", "absolute")
    absolute = agent_runs.policy_digest("claude")
    monkeypatch.chdir(tmp_path.parent)
    assert agent_runs.policy_digest("claude") == absolute != agent_runs.policy_digest("codex")


def test_policy_digest_tracks_policy(monkeypatch):
    base = agent_runs.policy_digest("codex")
    monkeypatch.setattr(agent_runs, "CODEX_EFFORT", "high")
    assert agent_runs.policy_digest("codex") != base
    claude = agent_runs.policy_digest("claude")
    monkeypatch.setattr(agent_runs, "ENV_ALLOW", agent_runs.ENV_ALLOW + ["EXTRA"])
    assert agent_runs.policy_digest("claude") != claude


def test_probe_binding_shape():
    b = agent_runs.probe_binding("claude", fake("commit"))
    assert b == {"cli_version": "fake-cli 1.0", "policy_digest": agent_runs.policy_digest("claude"),
                 "probe_digest": agent_runs.probe_digest(), "platform": agent_runs.sys.platform,
                 "read_scope": agent_runs.READ_SCOPE}
    assert agent_runs.probe_binding("codex", fake("commit"))["read_scope"] is None


# --- R21: attempt lifecycle ---------------------------------------------------------------

def make_attempt(env, status, trial=1, files=("transcript.jsonl",)):
    d = env["runs"] / "tiny-python-1" / "claude" / f"trial-{trial}" / "attempt-1"
    d.mkdir(parents=True)
    (d / "meta.json").write_text(json.dumps({"status": status, "error": "boom"}), encoding="utf-8")
    for f in files:
        (d / f).write_text("x\n", encoding="utf-8")
    return d


def plan(env):
    return agent_runs.plan_attempt(env["runs"], "tiny-python-1", "claude", 1)


def test_started_attempt_blocks_a_retry(env):
    d = make_attempt(env, "started")
    n, why = plan(env)
    assert n is None and "confirm --interrupted --stale-started" in why
    with pytest.raises(ValueError, match="stale-started"):
        agent_runs.confirm_interrupted(d, "process gone")
    assert not (d / "interrupted.json").exists()


def test_stale_started_is_finalised_then_confirmed(env):
    d = make_attempt(env, "started")
    rec = agent_runs.confirm_interrupted(d, "process gone, no python.exe running", stale_started=True)
    meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
    assert meta["status"] == "incomplete" and meta["error"] == "stale started attempt finalised by reviewer"
    assert rec["status_before"] == "started" and rec["confirmed_by"] == "reviewer"
    assert plan(env) == (2, "")


def test_incomplete_needs_interrupted_json_for_a_retry(env):
    d = make_attempt(env, "incomplete")
    n, why = plan(env)
    assert n is None and "interrupted.json" in why
    rec = agent_runs.confirm_interrupted(d, "grader crashed; transcript kept")
    assert sorted(x["path"] for x in rec["salvaged"]) == ["meta.json", "transcript.jsonl"]
    assert rec["unavailable"] == ["stderr.txt", "final.patch", "grade.json", "audit.json",
                                  "llm_grade.json", "classification.json"]
    assert plan(env) == (2, "")
    (d / "transcript.jsonl").write_text("tampered\n", encoding="utf-8")     # salvage no longer matches
    n, why = plan(env)
    assert n is None and "sha256 mismatch" in why


def test_confirm_interrupted_refuses_complete_and_blank_evidence(env):
    d = make_attempt(env, "complete")
    with pytest.raises(ValueError, match="complete"):
        agent_runs.confirm_interrupted(d, "x")
    d2 = make_attempt(env, "incomplete", trial=2)
    with pytest.raises(ValueError, match="evidence"):
        agent_runs.confirm_interrupted(d2, "  ")


def test_confirm_interrupted_cli_needs_no_source(env, capsys):
    d = make_attempt(env, "started")
    assert agent_runs.main(["confirm", str(d), "--interrupted", "--evidence", "gone"]) == 1
    assert agent_runs.main(["confirm", str(d), "--interrupted", "--stale-started",
                            "--evidence", "gone"]) == 0
    assert (d / "interrupted.json").is_file()
    assert agent_runs.main(["confirm", str(d), "--evidence", "x"]) == 1        # --source still needed
    assert agent_runs.main(["confirm", str(d), "--source", "agent", "--evidence", "x"]) == 1


def test_policy_digest_is_pinned_to_the_run_platform(monkeypatch):
    """A probe bound on Windows must verify from a Linux checkout: the digest takes the
    platform the run was made on, not the checker's."""
    win = agent_runs.policy_digest("codex", "win32")
    lin = agent_runs.policy_digest("codex", "linux")
    assert win != lin  # codex adds windows.sandbox on Windows
    monkeypatch.setattr(agent_runs.sys, "platform", "linux")
    assert agent_runs.policy_digest("codex", "win32") == win


# --- alts go through the same enforced command and gate ----------------------------------

def alt_argv(env, launcher, *extra):
    return ["alt", "--tasks-dir", str(env["tasks_dir"]), "--runs-dir", str(env["runs"]),
            "--task", "tiny-python-1", "--agent", "claude", "--label", "alt-2",
            "--exe", "claude=" + str(launcher), *extra]


def test_alt_refuses_without_a_good_probe(env, capsys):
    assert agent_runs.main(alt_argv(env, fake_launcher(env))) == 2
    assert "boundary probe missing" in capsys.readouterr().err
    assert not (env["runs"] / "_alts").exists()


def test_alt_runs_into_alts_with_the_constraint_recorded(env, capsys):
    install_probe(env)
    rc = agent_runs.main(alt_argv(env, fake_launcher(env), "--constraint", "Do not call list(v)."))
    out = capsys.readouterr().out
    adir = env["runs"] / "_alts" / "alt-2" / "tiny-python-1" / "claude" / "trial-1" / "attempt-1"
    meta = json.loads((adir / "meta.json").read_text(encoding="utf-8"))
    assert rc == 0 and "alt-2" in out and "PASS" in out
    assert meta["prompt"] == agent_runs.PROMPT + " Constraint: Do not call list(v)."
    assert meta["boundary"]["probe"]["_binding"]["policy_digest"] == agent_runs.policy_digest("claude")
    assert attempt_dirs(env) == []              # never lands in the scored layout
    agent_runs.main(alt_argv(env, fake_launcher(env)))
    assert (adir.parent / "attempt-2").is_dir()  # every alt attempt is kept


def test_alt_label_must_be_alt_n(env, capsys):
    install_probe(env)
    assert agent_runs.main(alt_argv(env, fake_launcher(env))[:-4] + ["--label", "reference",
                           "--exe", "claude=x"]) == 1


# --- R24: frozen evidence is never rewritten -------------------------------------------------

@pytest.mark.parametrize("status,confirm", [("incomplete", True), ("incomplete", False),
                                            ("started", False)])
def test_regrade_skips_frozen_attempts_even_with_all(env, capsys, status, confirm):
    d = make_attempt(env, status, files=("transcript.jsonl", "final.patch"))
    if confirm:
        agent_runs.confirm_interrupted(d, "grader crashed; patch salvaged")
    before = {p.name: p.read_bytes() for p in d.iterdir()}
    rc = agent_runs.main(["regrade", "--tasks-dir", str(env["tasks_dir"]),
                          "--runs-dir", str(env["runs"]), "--all"])
    assert rc == 0 and "skipped" in capsys.readouterr().out
    assert {p.name: p.read_bytes() for p in d.iterdir()} == before
    for writer in (agent_runs.regrade_attempt, agent_runs.reaudit_attempt):
        with pytest.raises(ValueError, match="frozen"):
            writer(env["task"], d, force=True)
