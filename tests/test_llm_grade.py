"""llm_grade.grade_attempt: always writes llm_grade.json, hash-bound, no real model calls."""
import json

import pytest

from harness import llm_grade
from harness.tasks import load_task


@pytest.fixture
def env(tmp_path, fixtures_dir):
    task = load_task(fixtures_dir / "tiny-python-1")
    adir = tmp_path / "attempt-1"
    adir.mkdir()
    (adir / "meta.json").write_text(json.dumps({"status": "complete"}), encoding="utf-8")
    return task, adir


def read(adir):
    return json.loads((adir / "llm_grade.json").read_text(encoding="utf-8"))


def test_empty_patch_is_not_graded_and_recorded(env, monkeypatch):
    task, adir = env
    (adir / "final.patch").write_bytes(b"")
    monkeypatch.setattr(llm_grade, "call_claude", lambda *a, **k: pytest.fail("model must not be called"))
    assert llm_grade.grade_attempt(task, adir).startswith("not_graded")
    data = read(adir)
    assert data["status"] == "not_graded" and data["reason"] == "empty patch"
    assert data["model"] == llm_grade.MODEL and len(data["patch_sha256"]) == 64


def test_graded_run_is_bound_and_skipped_when_current(env, monkeypatch):
    task, adir = env
    (adir / "final.patch").write_bytes(b"diff --git a/x b/x\n")
    calls = []
    monkeypatch.setattr(llm_grade, "call_claude",
                        lambda prompt, exe=None: calls.append(prompt) or {"verdict": "PASS", "raw_text": "x"})
    assert llm_grade.grade_attempt(task, adir) == "graded: PASS"
    data = read(adir)
    assert data["status"] == "graded" and data["verdict"] == "PASS"
    assert {"patch_sha256", "task_hash", "grader_prompt_sha256", "model"} <= set(data)
    assert llm_grade.grade_attempt(task, adir).startswith("skipped")
    assert len(calls) == 1
    # a different patch invalidates the binding and grades again
    (adir / "final.patch").write_bytes(b"diff --git a/y b/y\n")
    assert llm_grade.grade_attempt(task, adir) == "graded: PASS" and len(calls) == 2
    # force always re-grades
    llm_grade.grade_attempt(task, adir, force=True)
    assert len(calls) == 3


def test_stale_marker_forces_regrade(env, monkeypatch):
    task, adir = env
    (adir / "final.patch").write_bytes(b"diff\n")
    monkeypatch.setattr(llm_grade, "call_claude", lambda *a, **k: {"verdict": "FAIL"})
    llm_grade.grade_attempt(task, adir)
    data = read(adir)
    data["stale"] = True
    (adir / "llm_grade.json").write_text(json.dumps(data), encoding="utf-8")
    assert llm_grade.grade_attempt(task, adir) == "graded: FAIL"
    assert "stale" not in read(adir)


def test_model_failure_is_recorded_as_error_and_retried(env, monkeypatch):
    task, adir = env
    (adir / "final.patch").write_bytes(b"diff\n")

    def boom(*a, **k):
        raise RuntimeError("claude failed (exit 1)")

    monkeypatch.setattr(llm_grade, "call_claude", boom)
    assert llm_grade.grade_attempt(task, adir).startswith("error")
    data = read(adir)
    assert data["status"] == "error" and "claude failed" in data["error"]
    monkeypatch.setattr(llm_grade, "call_claude", lambda *a, **k: {"verdict": "PASS"})
    assert llm_grade.grade_attempt(task, adir) == "graded: PASS"      # an error is never "current"


def test_incomplete_attempt_is_skipped(env, monkeypatch):
    task, adir = env
    (adir / "meta.json").write_text(json.dumps({"status": "incomplete"}), encoding="utf-8")
    monkeypatch.setattr(llm_grade, "call_claude", lambda *a, **k: pytest.fail("no call"))
    assert "not complete" in llm_grade.grade_attempt(task, adir)
    assert not (adir / "llm_grade.json").exists()


def test_final_attempts_selects_last_or_all(tmp_path):
    for n in (1, 2):
        (tmp_path / "t1" / "claude" / "trial-1" / f"attempt-{n}").mkdir(parents=True)
    assert [d.name for d in llm_grade.final_attempts(tmp_path, "t1")] == ["attempt-2"]
    assert len(list(llm_grade.final_attempts(tmp_path, "t1", all_attempts=True))) == 2
