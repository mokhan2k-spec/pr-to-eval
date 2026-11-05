"""report --write / --check against fabricated run evidence (no agents)."""
import json
import shutil

import pytest

from harness import agent_runs, llm_grade, report
from harness.agent_runs import grader_digest, sha256_bytes, sha256_file, task_auditor_digest, task_hash
from harness.tasks import load_task


def jw(path, data):
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def fabricate(runs, task, agent, trial, attempt=1, verdict="PASS", source=None, patch=b"diff\n",
              confirmed=True, llm="graded", clean=True, outcome=None):
    """Write one fully consistent attempt dir."""
    d = runs / task.id / agent / f"trial-{trial}" / f"attempt-{attempt}"
    d.mkdir(parents=True)
    (d / "transcript.jsonl").write_text('{"type":"system"}\n', encoding="utf-8")
    (d / "final.patch").write_bytes(patch)
    jw(d / "meta.json", {"status": "complete", "task": task.id, "agent": agent, "trial": trial, "attempt": attempt,
                         "cli_version": "fake 1", "requested_model": "m", "observed_model": "m",
                         "timeout_s": 1200, "requested_effort": None,
                         "read_scope": "relative" if agent == "claude" else None})
    psha = sha256_bytes(patch)
    jw(d / "grade.json", {"verdict": verdict, "patch_sha256": psha, "task_hash": task_hash(task),
                          "grader_digest": grader_digest(task), "tests": {"failed": []}, "checks": {"failed": []}})
    base = {"patch_sha256": psha, "task_hash": task_hash(task), "model": llm_grade.MODEL,
            "grader_prompt_sha256": sha256_file(task.path / "grader_prompt.md")}
    if llm == "graded":
        jw(d / "llm_grade.json", {"status": "graded", "verdict": verdict if verdict != "ERROR" else "FAIL", **base})
    else:
        jw(d / "llm_grade.json", {"status": "not_graded", "reason": "empty patch", **base})
    jw(d / "audit.json", {"clean": clean, "hits": [] if clean else [{"kind": "path", "where": "w", "text": "t"}],
                          "transcript_sha256": sha256_file(d / "transcript.jsonl"), "patch_sha256": psha,
                          "review_required": [], "auditor_digest": task_auditor_digest(task)[0]})
    out = outcome or {"PASS": "pass", "FAIL": "fail", "ERROR": "error"}[verdict]
    jw(d / "classification.json", {
        "outcome": out, "failure_source": source if out != "pass" else None,
        "evidence": "quoted lines" if out != "pass" else "all passed", "classified_by": "main-seat",
        "confirmed": confirmed, "patch_sha256": psha, "grade_sha256": sha256_file(d / "grade.json"),
        "audit_sha256": sha256_file(d / "audit.json")})
    return d


@pytest.fixture
def world(tmp_path, fixtures_dir):
    task = load_task(fixtures_dir / "tiny-python-1")
    corpus = tmp_path / "corpus.json"
    jw(corpus, {"tasks": [{"id": "tiny-python-1", "language": "python"}], "min_flawed": 3, "min_alt": 1})
    runs = tmp_path / "runs"
    for agent in agent_runs.AGENTS:
        for n in range(1, 6):
            if (agent, n) == ("codex", 5):
                fabricate(runs, task, agent, n, verdict="FAIL", source="agent", patch=b"",
                          llm="not_graded")
            else:
                fabricate(runs, task, agent, n)
    report.write_summary(runs)
    return {"runs": runs, "task": task, "corpus": corpus, "tasks_dir": fixtures_dir, "tmp": tmp_path}


def check(w):
    return report.check(w["runs"], w["tasks_dir"], w["corpus"])


def attempt(w, agent="claude", trial=1, n=1):
    return w["runs"] / w["task"].id / agent / f"trial-{trial}" / f"attempt-{n}"


def test_complete_evidence_passes(world):
    assert check(world) == []
    assert report.main(["--check", "--runs-dir", str(world["runs"]), "--tasks-dir", str(world["tasks_dir"]),
                        "--corpus", str(world["corpus"])]) == 0


def test_summary_content(world):
    text = (world["runs"] / "SUMMARY.md").read_text(encoding="utf-8")
    assert "| tiny-python-1 | claude | 5/5 | 0 | 0 | 0 | 0 |" in text
    assert "| tiny-python-1 | codex | 4/5 | 1 | 0 | 0 | 0 |" in text
    assert "Not graded (empty patch): 1" in text
    assert "Graded runs compared: 9; agree: 9; disagree: 0" in text
    assert "## Run settings" in text and "fake 1" in text
    assert "tiny-python-1/codex/trial-5" in text            # the failure row


def test_check_is_strictly_read_only(world):
    before = {p: p.read_bytes() for p in world["runs"].rglob("*") if p.is_file()}
    (world["runs"] / "SUMMARY.md").write_bytes(b"drift\n")
    before[world["runs"] / "SUMMARY.md"] = b"drift\n"
    assert any("SUMMARY.md" in p for p in check(world))
    after = {p: p.read_bytes() for p in world["runs"].rglob("*") if p.is_file()}
    assert before == after


def test_missing_trial(world):
    shutil.rmtree(world["runs"] / "tiny-python-1" / "claude" / "trial-3")
    probs = check(world)
    assert any("trial-3: missing trial" in p for p in probs)


@pytest.mark.parametrize("name", ["transcript.jsonl", "final.patch", "meta.json", "grade.json",
                                  "llm_grade.json", "audit.json", "classification.json"])
def test_missing_artifact(world, name):
    (attempt(world) / name).unlink()
    assert any(f"missing {name}" in p or f"missing or invalid {name}" in p for p in check(world))


def test_unconfirmed_failure(world):
    d = attempt(world, "codex", 5)
    cls = json.loads((d / "classification.json").read_text(encoding="utf-8"))
    cls["confirmed"] = False
    jw(d / "classification.json", cls)
    assert any("not confirmed" in p for p in check(world))


def test_summary_drift_and_missing(world):
    (world["runs"] / "SUMMARY.md").write_text("old\n", encoding="utf-8")
    assert any("differs from the render" in p for p in check(world))
    (world["runs"] / "SUMMARY.md").unlink()
    assert any("SUMMARY.md: missing" in p for p in check(world))


def test_stale_task_hash_detected(world):
    task_dir = world["task"].path
    # simulate a task edit without touching the repo fixture: bump the hash binding instead
    d = attempt(world)
    g = json.loads((d / "grade.json").read_text(encoding="utf-8"))
    g["task_hash"] = "0" * 64
    jw(d / "grade.json", g)
    probs = check(world)
    assert any("grade.json task_hash is stale" in p for p in probs)
    assert any("classification.json grade_sha256" in p for p in probs)   # dependent hash broke too
    assert task_dir.exists()


def test_patch_swap_detected(world):
    (attempt(world) / "final.patch").write_bytes(b"tampered\n")
    probs = check(world)
    for want in ("grade.json patch_sha256", "llm_grade.json patch_sha256", "audit.json patch_sha256",
                 "classification.json patch_sha256"):
        assert any(want in p for p in probs), want


def test_transcript_swap_detected(world):
    (attempt(world) / "transcript.jsonl").write_text("{}\n", encoding="utf-8")
    assert any("audit.json transcript_sha256" in p for p in check(world))


def test_llm_grade_bindings(world):
    d = attempt(world)
    for key in ("grader_prompt_sha256", "model", "task_hash"):
        data = json.loads((d / "llm_grade.json").read_text(encoding="utf-8"))
        orig = data[key]
        data[key] = "other"
        jw(d / "llm_grade.json", data)
        assert any(f"llm_grade.json {key} is stale" in p for p in check(world)), key
        data[key] = orig
        jw(d / "llm_grade.json", data)
    assert check(world) == []


def test_llm_status_must_fit_patch(world):
    d = attempt(world)
    data = json.loads((d / "llm_grade.json").read_text(encoding="utf-8"))
    data["status"] = "not_graded"
    jw(d / "llm_grade.json", data)
    assert any("does not fit the patch" in p for p in check(world))
    data.update(status="error")
    jw(d / "llm_grade.json", data)
    assert any("status is 'error'" in p for p in check(world))


def test_classification_must_match_verdict(world):
    d = attempt(world)
    cls = json.loads((d / "classification.json").read_text(encoding="utf-8"))
    cls.update(outcome="fail", failure_source="agent", evidence="x")
    jw(d / "classification.json", cls)
    assert any("inconsistent with verdict" in p for p in check(world))


def test_non_pass_needs_source_and_evidence(world):
    d = attempt(world, "codex", 5)
    cls = json.loads((d / "classification.json").read_text(encoding="utf-8"))
    cls.update(failure_source=None)
    jw(d / "classification.json", cls)
    assert any("without a failure_source" in p for p in check(world))
    cls.update(failure_source="agent", evidence="  ")
    jw(d / "classification.json", cls)
    assert any("blank evidence" in p for p in check(world))
    cls.update(failure_source="somebody")
    jw(d / "classification.json", cls)
    assert any("without a failure_source" in p for p in check(world))


def test_dirty_audit_requires_contaminated_or_override(world):
    d = attempt(world)
    aud = json.loads((d / "audit.json").read_text(encoding="utf-8"))
    aud.update(clean=False, hits=[{"kind": "string", "where": "w", "text": "t"}])
    jw(d / "audit.json", aud)
    cls = json.loads((d / "classification.json").read_text(encoding="utf-8"))
    cls["audit_sha256"] = sha256_file(d / "audit.json")
    jw(d / "classification.json", cls)
    assert any("audit is not clean" in p for p in check(world))
    cls["audit_override"] = True
    jw(d / "classification.json", cls)
    assert not any("audit is not clean" in p for p in check(world))


def test_contaminated_final_attempt_is_allowed_with_evidence(world):
    shutil.rmtree(world["runs"] / "tiny-python-1" / "claude" / "trial-2")
    fabricate(world["runs"], world["task"], "claude", 2, verdict="PASS", outcome="contaminated",
              clean=False)
    report.write_summary(world["runs"])
    assert check(world) == []
    assert "| tiny-python-1 | claude | 4/5 | 0 | 0 | 0 | 1 |" in (world["runs"] / "SUMMARY.md").read_text(encoding="utf-8")


def test_retry_chain_rules(world):
    runs, task = world["runs"], world["task"]
    shutil.rmtree(runs / "tiny-python-1" / "claude" / "trial-1")
    fabricate(runs, task, "claude", 1, attempt=1, verdict="ERROR", source="environment")
    fabricate(runs, task, "claude", 1, attempt=2)
    report.write_summary(runs)
    assert check(world) == []
    # an earlier attempt that was an agent failure must not have been retried
    cls = json.loads((attempt(world, n=1) / "classification.json").read_text(encoding="utf-8"))
    cls.update(failure_source="agent", outcome="fail")
    jw(attempt(world, n=1) / "classification.json", cls)
    assert any("non-final attempt is not confirmed environment/contaminated" in p for p in check(world))
    # unconfirmed earlier attempt
    cls.update(failure_source="environment", confirmed=False)
    jw(attempt(world, n=1) / "classification.json", cls)
    assert any("non-final attempt" in p for p in check(world))
    # third attempt
    fabricate(runs, task, "claude", 1, attempt=3)
    assert any("more than 2 attempts" in p for p in check(world))


def test_stale_classification_flagged(world):
    d = attempt(world)
    cls = json.loads((d / "classification.json").read_text(encoding="utf-8"))
    cls["stale"] = True
    jw(d / "classification.json", cls)
    assert any("classification.json is marked stale" in p for p in check(world))


def test_corpus_task_without_dir_and_missing_corpus(world, tmp_path):
    jw(world["corpus"], {"tasks": [{"id": "ghost-1", "language": "python"}]})
    assert any("ghost-1" in p for p in check(world))
    assert report.check(world["runs"], world["tasks_dir"], tmp_path / "nope.json")[0].startswith("corpus:")


def test_boundary_dir_is_not_a_trial(world):
    (world["runs"] / "_boundary").mkdir()
    jw(world["runs"] / "_boundary" / "probe.json", {"agents": {}})
    report.write_summary(world["runs"])
    assert check(world) == []


# --- R13 / R14 / R15 ---------------------------------------------------------------------

def test_editing_grade_py_makes_check_fail_as_stale(world, tmp_path, monkeypatch):
    # point the grader digest at a private copy of the harness so the real grade.py stays untouched
    from pathlib import Path
    copy = tmp_path / "harness_copy"
    copy.mkdir()
    for name in ("grade.py", "workspace.py"):
        shutil.copy(Path(agent_runs.__file__).parent / name, copy / name)
    monkeypatch.setattr(agent_runs, "HARNESS_DIR", copy)
    # re-bind every grade.json to the copy's digest, then confirm --check is green
    for g in world["runs"].rglob("grade.json"):
        data = json.loads(g.read_text(encoding="utf-8"))
        data["grader_digest"] = grader_digest(world["task"])
        jw(g, data)
        cls = g.with_name("classification.json")
        c = json.loads(cls.read_text(encoding="utf-8"))
        c["grade_sha256"] = sha256_file(g)
        jw(cls, c)
    assert check(world) == []
    (copy / "grade.py").write_text((copy / "grade.py").read_text(encoding="utf-8") + "\n# edited\n",
                                   encoding="utf-8")
    probs = check(world)
    assert any("grade.json grader_digest is stale" in p for p in probs)


def test_editing_audit_py_or_leak_ids_makes_check_fail(world, monkeypatch):
    from harness import audit
    monkeypatch.setattr(audit, "auditor_digest", lambda ids=None: "b" * 64)
    assert any("audit.json auditor_digest is stale" in p for p in check(world))


def test_superseded_attempt_with_missing_patch_fails_check(world):
    runs, task = world["runs"], world["task"]
    shutil.rmtree(runs / "tiny-python-1" / "claude" / "trial-1")
    fabricate(runs, task, "claude", 1, attempt=1, verdict="ERROR", source="environment")
    fabricate(runs, task, "claude", 1, attempt=2)
    report.write_summary(runs)
    assert check(world) == []
    (attempt(world, n=1) / "final.patch").unlink()
    assert any("attempt-1: missing final.patch" in p for p in check(world))


def test_superseded_attempt_bindings_are_checked(world):
    runs, task = world["runs"], world["task"]
    shutil.rmtree(runs / "tiny-python-1" / "claude" / "trial-1")
    fabricate(runs, task, "claude", 1, attempt=1, verdict="ERROR", source="environment")
    fabricate(runs, task, "claude", 1, attempt=2)
    report.write_summary(runs)
    (attempt(world, n=1) / "final.patch").write_bytes(b"tampered\n")
    probs = check(world)
    assert any("attempt-1: grade.json patch_sha256" in p for p in probs)


def test_incomplete_attempts(world):
    runs, task = world["runs"], world["task"]
    shutil.rmtree(runs / "tiny-python-1" / "claude" / "trial-1")
    d = runs / "tiny-python-1" / "claude" / "trial-1" / "attempt-1"
    d.mkdir(parents=True)
    jw(d / "meta.json", {"status": "incomplete", "error": "Traceback...\nRuntimeError: boom"})
    fabricate(runs, task, "claude", 1, attempt=2)
    report.write_summary(runs)
    assert check(world) == []                                 # superseded incomplete is allowed
    text = (runs / "SUMMARY.md").read_text(encoding="utf-8")
    assert "### Incomplete attempts" in text and "RuntimeError: boom" in text
    # as the final attempt it is a problem; a never-finalised one is too
    shutil.rmtree(runs / "tiny-python-1" / "claude" / "trial-1" / "attempt-2")
    assert any("final attempt is incomplete" in p for p in check(world))
    jw(d / "meta.json", {"status": "started"})
    assert any("never finalised" in p for p in check(world))


def test_all_attempts_section_lists_contamination_followed_by_clean_retry(world):
    runs, task = world["runs"], world["task"]
    shutil.rmtree(runs / "tiny-python-1" / "claude" / "trial-2")
    fabricate(runs, task, "claude", 2, attempt=1, outcome="contaminated", clean=False)
    fabricate(runs, task, "claude", 2, attempt=2)
    report.write_summary(runs)
    text = (runs / "SUMMARY.md").read_text(encoding="utf-8")
    assert "## All attempts" in text and "tiny-python-1/claude/trial-2/attempt-1 | no |" in text
    assert "NOT prevented" in text
    assert check(world) == []


def test_review_required_must_be_cleared(world):
    d = attempt(world)
    aud = json.loads((d / "audit.json").read_text(encoding="utf-8"))
    aud["review_required"] = [{"where": "patch a.py:1", "text": "import socket", "pattern": "socket"}]
    jw(d / "audit.json", aud)
    cls = json.loads((d / "classification.json").read_text(encoding="utf-8"))
    cls["audit_sha256"] = sha256_file(d / "audit.json")
    jw(d / "classification.json", cls)
    assert any("not cleared at confirm" in p for p in check(world))
    cls["review_cleared"] = "stdlib import in a test double"
    jw(d / "classification.json", cls)
    assert not any("not cleared" in p for p in check(world))
