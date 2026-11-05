"""privacy_check: hits, allowlist, and R17 (an incomplete scan must not pass)."""
import json
import os
import subprocess
import sys

import pytest

from harness import privacy_check as pc

NAME = "muhammed"            # the rule is on the username; built here so the test is not a hit itself


@pytest.fixture
def repo(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    (tmp_path / "ok.txt").write_text("nothing personal here\n", encoding="utf-8")
    (tmp_path / "allow.json").write_text("[]", encoding="utf-8")
    return tmp_path


def run(repo, *extra):
    return pc.main(["--root", str(repo), "--allowlist", str(repo / "allow.json"), *extra])


def test_clean_repo_passes_and_prints_scope(repo, capsys):
    assert run(repo) == 0
    out = capsys.readouterr().out
    assert "0 unallowlisted hit(s)" in out and "scope:" in out and "file(s) scanned" in out


def test_a_hit_fails_and_the_allowlist_clears_it(repo, capsys):
    (repo / "notes.txt").write_text(f"C:/Users/{NAME}/Desktop\n", encoding="utf-8")
    assert run(repo) == 1
    assert "notes.txt:1" in capsys.readouterr().out
    (repo / "allow.json").write_text(json.dumps(
        [{"path_glob": "notes.txt", "pattern": "*", "reason": "test"}]), encoding="utf-8")
    assert run(repo) == 0


def test_unreadable_file_makes_the_scan_fail(repo, capsys, monkeypatch):
    target = repo / "locked.txt"
    target.write_text("data\n", encoding="utf-8")
    real = pc.Path.read_bytes

    def read_bytes(self):
        if self.name == "locked.txt":
            raise PermissionError("locked by another process")
        return real(self)

    monkeypatch.setattr(pc.Path, "read_bytes", read_bytes)
    assert run(repo) == 2
    out = capsys.readouterr().out
    assert "SCAN FAILURE: unreadable file locked.txt" in out and "1 scan failure(s)" in out


def test_tracked_but_missing_file_is_a_failure(repo, capsys):
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)
    os.remove(repo / "ok.txt")
    assert run(repo) == 2
    assert "unreadable file ok.txt" in capsys.readouterr().out


def test_not_a_git_repo_is_a_failure_not_a_pass(tmp_path, capsys):
    (tmp_path / "allow.json").write_text("[]", encoding="utf-8")
    assert pc.main(["--root", str(tmp_path), "--allowlist", str(tmp_path / "allow.json")]) == 2
    assert "git ls-files failed" in capsys.readouterr().out


def test_history_empty_repo_is_verified_no_commits(repo, capsys):
    assert run(repo, "--history") == 0
    assert "no commits (verified)" in capsys.readouterr().out


def test_history_scans_commits_and_reports_the_count(repo, capsys):
    (repo / "leak.txt").write_text(f"/Users/{NAME}/x\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "one"], cwd=repo, check=True)
    (repo / "leak.txt").write_text("clean now\n", encoding="utf-8")
    subprocess.run(["git", "commit", "-aqm", "two"], cwd=repo, check=True)
    assert run(repo, "--history") == 1                    # gone from the tree, still in history
    out = capsys.readouterr().out
    assert "history:leak.txt" in out and "2 commit(s) scanned" in out


def test_git_log_failure_is_a_scan_failure(repo, capsys, monkeypatch):
    real = pc._git

    def fake(root, *args):
        if args[:1] == ("log",):
            return subprocess.CompletedProcess(args, 128, b"", b"fatal: bad object HEAD")
        return real(root, *args)

    (repo / "a.txt").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "one"], cwd=repo, check=True)
    monkeypatch.setattr(pc, "_git", fake)
    assert run(repo, "--history") == 2
    assert "git log failed (exit 128)" in capsys.readouterr().out


def test_rev_list_failure_is_a_scan_failure(repo, capsys, monkeypatch):
    real = pc._git

    def fake(root, *args):
        if args[:1] == ("rev-list",):
            return subprocess.CompletedProcess(args, 128, b"", b"fatal")
        return real(root, *args)

    monkeypatch.setattr(pc, "_git", fake)
    assert run(repo, "--history") == 2
    assert "git rev-list failed" in capsys.readouterr().out

