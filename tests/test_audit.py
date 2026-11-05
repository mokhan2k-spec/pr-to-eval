"""harness.audit on synthetic transcripts in both formats. No agents involved."""
import json

import pytest

from harness import audit


def claude(*blocks):
    return "\n".join(json.dumps({"type": "assistant", "message": {"content": [b]}}) for b in blocks)


def tool(name, **inp):
    return {"type": "tool_use", "id": "t", "name": name, "input": inp}


def codex(*items):
    return "\n".join(json.dumps({"type": "item.completed", "item": dict(i, id=f"i{n}")})
                     for n, i in enumerate(items))


def cmd(command):
    return {"type": "command_execution", "command": command, "aggregated_output": "", "exit_code": 0}


def kinds(res):
    return sorted({h["kind"] for h in res["hits"]})


# --- Claude stream-json -----------------------------------------------------

def test_claude_clean_run():
    t = claude(tool("Read", file_path="<WS>/tiny/mathx.py"),
               tool("Edit", file_path="<WS>/tiny/mathx.py", old_string="a", new_string="b"),
               tool("Read", file_path="tiny/mathx.py"),
               tool("Glob", pattern="**/*.py"),
               tool("Grep", pattern="def clamp", path="tiny"),
               tool("Bash", command="python -m pytest -q tiny 2>/dev/null"),
               {"type": "text", "text": "Fixed the upper bound."})
    res = audit.audit_transcript(t, slug="example/tiny")
    assert res["clean"] and res["hits"] == [] and res["tool_calls"] == 7


@pytest.mark.parametrize("path", [
    "C:\\Users\\bob\\secret.txt", "C:/Users/bob/secret.txt", "c:\\users\\BOB\\x", "/c/Users/bob/x",
    "/etc/passwd", "<HOME>/Documents/x", "<REPO>/tasks/t/hidden/test.py", "../secret.txt",
    "<WS>/../other/x", "a/../../x", "~/x",
])
def test_claude_read_outside_is_flagged(path):
    res = audit.audit_transcript(claude(tool("Read", file_path=path)))
    assert not res["clean"] and "path" in kinds(res), path


def test_claude_inside_dotdot_is_fine():
    res = audit.audit_transcript(claude(tool("Read", file_path="<WS>/a/../tiny/mathx.py")))
    assert res["clean"]


def test_claude_glob_and_grep_paths_are_checked():
    t = claude(tool("Glob", pattern="*.py", path="C:\\Users\\bob"),
               tool("Grep", pattern="x", path="/c/Users/bob/Code"),
               tool("Glob", pattern="../../**"))
    assert len([h for h in audit.audit_transcript(t)["hits"] if h["kind"] == "path"]) == 3


def test_claude_bash_paths_in_commands():
    t = claude(tool("Bash", command="python -m pytest ../other"),
               tool("Bash", command="git diff -- C:\\Users\\bob\\x.py"),
               tool("Bash", command="python -m pytest /tmp/foo/bar"))
    assert len(audit.audit_transcript(t)["hits"]) == 3


def test_forbidden_strings_in_tool_input_and_text():
    for what, block in [
        ("github.com", tool("Bash", command="curl https://github.com/x")),
        ("slug", tool("Grep", pattern="example/tiny")),
        ("/pull/", {"type": "text", "text": "see /pull/12"}),
        ("hidden_tests", tool("Bash", command="ls hidden_tests")),
        ("reference.patch", tool("Read", file_path="solutions/reference.patch")),
        ("solutions.json", tool("Read", file_path="<WS>/solutions.json")),
        ("expected_tests", tool("Read", file_path="expected_tests.json")),
        ("pr-to-eval", tool("Bash", command="echo pr-to-eval")),
    ]:
        res = audit.audit_transcript(claude(block), slug="example/tiny")
        assert "string" in kinds(res), what


def test_tool_results_are_not_scanned_for_strings():
    # file contents that mention github.com (licence headers, docs) must not count
    ev = {"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "t", "content": "see https://github.com/x/pull/3"}]}}
    assert audit.audit_transcript(json.dumps(ev))["clean"]


def test_canary_in_transcript_and_patch():
    res = audit.audit_transcript('{"x": "CANARY-123"}', canary="CANARY-123")
    assert kinds(res) == ["canary"]
    res = audit.audit_transcript("", patch="+x = 'CANARY-123'\n", canary="CANARY-123")
    assert [h["where"] for h in res["hits"]] == ["patch"]


def test_patch_added_lines_checked_for_strings():
    res = audit.audit_transcript("", patch="--- a/f\n+++ b/f\n+# see github.com/x\n-# github.com/old\n")
    assert len(res["hits"]) == 1 and res["hits"][0]["where"] == "patch"


def test_garbage_lines_are_skipped():
    assert audit.audit_transcript("not json\n{broken\n[1,2]\n")["clean"]


# --- Codex --json -----------------------------------------------------------

PS = '"C:\\WINDOWS\\System32\\WindowsPowerShell\\v1.0\\powershell.exe" -Command '


def test_codex_clean_run_with_powershell_wrapper():
    t = codex(cmd(PS + "'Get-Content tiny\\mathx.py'"),
              cmd(PS + "'python -m pytest -q'"),
              cmd(PS + "'Get-Content <WS>\\tiny\\mathx.py'"),
              {"type": "file_change", "changes": [{"path": "<WS>/tiny/mathx.py", "kind": "update"}]},
              {"type": "agent_message", "text": "Done."})
    res = audit.audit_transcript(t)
    assert res["clean"], res["hits"]


def test_codex_escapes_flagged():
    t = codex(cmd(PS + "'cat C:\\Users\\bob\\.ssh\\id_rsa'"),
              cmd(PS + "'Get-Content ..\\..\\other.txt'"),
              cmd("bash -lc 'cat /c/Users/bob/x'"),
              cmd(PS + "'type C:/Users/bob/x'"),
              {"type": "file_change", "changes": [{"path": "C:\\Users\\bob\\x.py", "kind": "add"}]})
    paths = [h for h in audit.audit_transcript(t)["hits"] if h["kind"] == "path"]
    assert len(paths) == 5


def test_codex_allowed_outside_locations():
    t = codex(cmd(PS + "'& C:\\Python313\\python.exe -m pytest'"),
              cmd(PS + "'<REPO>/node_modules/.bin/vitest run'"),
              cmd("cmd /c dir /b"),
              cmd(PS + "'python -m pytest 2>/dev/null'"),
              cmd(PS + "'node C:\\Users\\bob\\AppData\\Roaming\\npm\\x.js'"))
    res = audit.audit_transcript(t)
    assert res["clean"], res["hits"]


def test_codex_duplicate_items_counted_once():
    one = json.dumps({"type": "item.started", "item": dict(cmd("cat /etc/passwd"), id="a")})
    two = json.dumps({"type": "item.completed", "item": dict(cmd("cat /etc/passwd"), id="a")})
    res = audit.audit_transcript(one + "\n" + two)
    assert len(res["hits"]) == 1


def test_codex_forbidden_string_in_command_and_message():
    t = codex(cmd(PS + "'git log --all'"), cmd(PS + "'curl https://github.com/a/b'"),
              {"type": "agent_message", "text": "looking at the upstream PR /pull/9"})
    assert len([h for h in audit.audit_transcript(t)["hits"] if h["kind"] == "string"]) == 2


def test_cli_exit_code(tmp_path):
    f = tmp_path / "t.jsonl"
    f.write_text(claude(tool("Read", file_path="/etc/passwd")), encoding="utf-8")
    assert audit.main([str(f)]) == 1
    f.write_text(claude(tool("Read", file_path="a.py")), encoding="utf-8")
    assert audit.main([str(f)]) == 0


# --- R11: hidden identifiers and host-access scan ----------------------------------------

def make_task(tmp_path, hidden, upstream="", task_md="fix it"):
    from harness.tasks import Task
    (tmp_path / "hidden").mkdir()
    (tmp_path / "upstream").mkdir()
    (tmp_path / "hidden" / "test_h.py").write_text(hidden, encoding="utf-8")
    (tmp_path / "upstream" / "m.py").write_text(upstream, encoding="utf-8")
    (tmp_path / "TASK.md").write_text(task_md, encoding="utf-8")
    return Task(tmp_path, {"id": "t"})


HIDDEN = '''
def test_secret_behaviour_xyz():
    assert f("a distinctive literal here") == 1
    assert f("shared text in task") == 2
    assert f("vitest/config") == 3
'''


def test_leak_identifiers_exclude_task_and_upstream_text(tmp_path):
    task = make_task(tmp_path, HIDDEN, upstream="# a distinctive literal here\n", task_md="shared text in task")
    ids = audit.leak_identifiers(task)
    assert "test_secret_behaviour_xyz" in ids
    assert "a distinctive literal here" not in ids       # already in upstream
    assert "shared text in task" not in ids              # already in TASK.md
    assert "vitest/config" not in ids                    # import path


def test_hidden_identifier_in_transcript_or_patch_is_contamination():
    ids = ["test_secret_behaviour_xyz"]
    t = claude(tool("Write", file_path="a.py", content="def test_secret_behaviour_xyz(): pass"))
    res = audit.audit_transcript(t, leak_ids=ids)
    assert not res["clean"] and kinds(res) == ["hidden-identifier"]
    res = audit.audit_transcript("", patch="+def test_secret_behaviour_xyz(): pass\n", leak_ids=ids)
    assert kinds(res) == ["hidden-identifier"]
    assert audit.audit_transcript(claude(tool("Read", file_path="a.py")), leak_ids=ids)["clean"]


@pytest.mark.parametrize("text,pattern", [
    ("p = Path.home() / 'x'", "home-dir"), ("import socket", "socket"),
    ("urllib.request.urlopen(u)", "urllib"), ("subprocess.run(['ls'])", "subprocess"),
    ("open('C:/Users/x/y')", "absolute-path"), ("open('../secret')", "parent-path"),
    ("for r, d, f in os.walk('/etc'):", "os.walk"), ("import requests", "requests"),
])
def test_python_static_scan(text, pattern):
    hits = audit.scan_text("f.py", text, audit._patterns_for("f.py"))
    assert pattern in {h["pattern"] for h in hits}, text


@pytest.mark.parametrize("text,pattern", [
    ("os.homedir()", "os.homedir"), ("fs.readFileSync('C:/Users/x')", "fs-outside"),
    ("fs.readFileSync('../x')", "fs-outside"), ("require('child_process')", "child_process"),
    ("await fetch(url)", "fetch"), ("https.request(opts)", "http-request"),
    ("import net from 'node:net'", "net"),
])
def test_js_static_scan(text, pattern):
    hits = audit.scan_text("f.ts", text, audit._patterns_for("f.ts"))
    assert pattern in {h["pattern"] for h in hits}, text


def test_benign_code_has_no_review_hits():
    code = "def clamp(v, lo, hi):\n    return max(lo, min(v, hi))\nfor r, d, f in os.walk('.'):\n    pass\n"
    assert audit.scan_text("f.py", code, audit._patterns_for("f.py")) == []


def test_review_required_sets_no_contamination_and_quotes_the_line():
    patch = "--- a/t.py\n+++ b/t.py\n+import socket\n+x = 1\n"
    res = audit.audit_transcript("", patch=patch)
    assert res["clean"] and res["hits"] == []
    assert res["review_required"][0]["text"] == "import socket"
    assert "patch t.py:1" in res["review_required"][0]["where"]


def test_commands_and_written_files_are_scanned():
    t = claude(tool("Bash", command="curl https://example.org"),
               tool("Write", file_path="probe.py", content="import urllib.request\n"))
    res = audit.audit_transcript(t)
    pats = {h["pattern"] for h in res["review_required"]}
    assert {"network-tool", "urllib"} <= pats


def test_auditor_digest_depends_on_leak_ids():
    assert audit.auditor_digest(["a"]) != audit.auditor_digest(["b"])
    assert audit.auditor_digest(["b", "a"]) == audit.auditor_digest(["a", "b"])


def test_colon_plus_escaped_newline_is_not_a_drive_path():
    code = "for subv in v:" + chr(92) + "n    self_insert(k, subv)" + chr(92) + "n    x = 1"
    assert audit.audit_transcript(claude(tool("Bash", command="python - <<'E'\n" + code + "\nE")))["hits"] == []
    real = "type " + "C:" + chr(92) + "new" + chr(92) + "x.txt"
    assert audit.audit_transcript(claude(tool("Bash", command=real)))["hits"]
