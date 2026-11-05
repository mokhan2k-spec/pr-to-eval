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
        [{"path_glob": "notes.txt", "rule": ["windows-user-path", "unix-users-path", "username"], "pattern": "^.+$", "reason": "test"}]), encoding="utf-8")
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


# --- R22: allowlist entries are scoped to rule + path + detected value --------------------

FAKE_OK = "someone@" + "example.com"            # reserved domain (allowlisted by value)
FAKE_REAL = "real.person@" + "gmail.com"         # obviously fake, but not allowlisted
ALLOW_EXAMPLE = {"path_glob": "*", "rule": "email", "pattern": "^[^@\\s]+@example[.]com$",
                 "reason": "reserved example domain"}


def test_allowlisted_value_on_its_own_passes():
    assert pc.scan_text("a.json", f'{{"to": "{FAKE_OK}"}}', [ALLOW_EXAMPLE]) == []


def test_allowlisted_value_does_not_hide_a_real_one_on_the_same_line():
    hits = pc.scan_text("a.json", f'{{"to": "{FAKE_OK}", "cc": "{FAKE_REAL}"}}', [ALLOW_EXAMPLE])
    assert [(h[2], h[3]) for h in hits] == [("email", FAKE_REAL)]


def test_a_rule_mismatch_does_not_exempt():
    entry = dict(ALLOW_EXAMPLE, rule="phone")
    assert [h[3] for h in pc.scan_text("a.json", FAKE_OK, [entry])] == [FAKE_OK]
    entry = dict(ALLOW_EXAMPLE, rule="username")
    assert pc.scan_text("a.json", f"{FAKE_OK} {NAME}", [dict(ALLOW_EXAMPLE), entry])[0][2] == "username"


def test_path_glob_mismatch_does_not_exempt():
    entry = dict(ALLOW_EXAMPLE, path_glob="docs/*")
    assert pc.scan_text("a.json", FAKE_OK, [entry])
    assert pc.scan_text("docs/a.md", FAKE_OK, [entry]) == []


LINE = "Author: Jane Doe <jane@" + "example.org>"
EXACT = {"path_glob": "git-log", "mode": "exact_line", "rule": "email",
         "pattern": "^Author: Jane Doe <jane@example[.]org>$", "reason": "fake owner line"}


def test_exact_line_matches_only_the_whole_line():
    assert pc.is_allowed([EXACT], "git-log", "email", "jane@example.org", LINE)
    assert not pc.is_allowed([EXACT], "git-log", "email", "jane@example.org", LINE + " and more")
    assert not pc.is_allowed([EXACT], "other", "email", "jane@example.org", LINE)
    assert not pc.is_allowed([EXACT], "git-log", "phone", "jane@example.org", LINE)


@pytest.mark.parametrize("pattern", ["Author: Jane", "^Author: Jane", "Author: Jane$", "^Author: Jane\\$"])
def test_exact_line_without_anchors_is_rejected(tmp_path, pattern):
    f = tmp_path / "allow.json"
    f.write_text(json.dumps([dict(EXACT, pattern=pattern)]), encoding="utf-8")
    with pytest.raises(ValueError, match="anchored"):
        pc.load_allowlist(f)


@pytest.mark.parametrize("bad", [
    {"path_glob": "*", "pattern": "^x$", "reason": "r"},                         # no rule
    {"path_glob": "*", "rule": "email", "pattern": "*", "reason": "r"},          # not a regex
    {"path_glob": "*", "rule": "no-such-rule", "pattern": "^x$", "reason": "r"},
    {"path_glob": "*", "rule": "email", "pattern": "^x$", "reason": " "},
    {"path_glob": "*", "rule": "email", "pattern": "^x$", "reason": "r", "mode": "line"},
])
def test_unscoped_or_malformed_entries_fail_loudly(tmp_path, bad):
    f = tmp_path / "allow.json"
    f.write_text(json.dumps([bad]), encoding="utf-8")
    with pytest.raises(ValueError):
        pc.load_allowlist(f)


def test_the_shipped_allowlist_is_valid():
    assert pc.load_allowlist()


def test_history_exact_line_entry_exempts_only_that_author(repo, capsys):
    subprocess.run(["git", "config", "user.name", "Jane Doe"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "jane@" + "example.org"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "one"], cwd=repo, check=True)
    assert run(repo, "--history") == 1                       # the author email is a hit
    (repo / "allow.json").write_text(json.dumps([EXACT]), encoding="utf-8")
    assert run(repo, "--history") == 0
