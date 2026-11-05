"""Grade a workspace: hidden tests (JUnit XML) + shell checks. No LLM involved."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

from .tasks import Task
from .workspace import ApplyError, make_workspace, patch_is_empty, remove_tree


@dataclass
class RunResult:
    returncode: int | None
    stdout: str
    stderr: str
    timed_out: bool


def kill_tree(proc: subprocess.Popen) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                       capture_output=True, check=False)
    else:
        import signal
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    try:
        proc.kill()
    except OSError:
        pass


def run_process(cmd, cwd, env=None, timeout=None, stdout=None, stderr=None,
                stdin=subprocess.DEVNULL, input_text: str | None = None) -> RunResult:
    """Run cmd with a wall-clock limit, killing the whole process tree on timeout.
    stdout/stderr may be open file objects; otherwise they are captured."""
    kw = {}
    if os.name == "nt":
        kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True
    if input_text is not None:
        stdin = subprocess.PIPE
    proc = subprocess.Popen(
        cmd, cwd=cwd, env=env, stdin=stdin,
        stdout=stdout if stdout is not None else subprocess.PIPE,
        stderr=stderr if stderr is not None else subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace", **kw,
    )
    timed_out = False
    try:
        out, err = proc.communicate(input=input_text, timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        kill_tree(proc)
        try:
            out, err = proc.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            out, err = "", ""
    return RunResult(proc.returncode, out or "", err or "", timed_out)


def find_bash() -> str:
    """Git for Windows bash if present (System32 bash.exe is WSL); else PATH bash."""
    if os.name == "nt":
        git = shutil.which("git")
        if git:
            here = Path(git).resolve()
            for parent in list(here.parents)[:4]:
                for rel in (("bin", "bash.exe"), ("usr", "bin", "bash.exe")):
                    cand = parent.joinpath(*rel)
                    if cand.exists():
                        return str(cand)
    return shutil.which("bash") or "bash"


def resolve_cmd(cmd: list[str], junit: Path) -> list[str]:
    out = [a.replace("{junit}", str(junit)) for a in cmd]
    first = out[0]
    if first in ("python", "python3"):
        out[0] = sys.executable
    elif first in ("npx", "npm", "node"):
        out[0] = shutil.which(first) or first
    return out


def parse_junit(path: Path) -> dict:
    """Parse a JUnit XML file. Test id = '<classname>::<name>'.

    Returns {"ok", "total", "passed", "failed", "skipped", "ids"}. ok is False if the
    file is missing or does not parse. total counts every case, skipped ones included;
    a skipped case is not passed. `failed` holds failed and errored cases."""
    out = {"ok": False, "total": 0, "passed": 0, "failed": [], "skipped": [], "ids": []}
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, FileNotFoundError, OSError):
        return out
    out["ok"] = True
    for case in root.iter("testcase"):
        cid = f"{case.get('classname', '')}::{case.get('name', '')}"
        out["total"] += 1
        out["ids"].append(cid)
        if case.find("failure") is not None or case.find("error") is not None:
            out["failed"].append(cid)
        elif case.find("skipped") is not None:
            out["skipped"].append(cid)
        else:
            out["passed"] += 1
    return out


def _env_for(ws: Path) -> dict:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ws)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONUTF8"] = "1"
    env["PYTHON"] = sys.executable
    env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", "")
    return env


def scrub(text: str, ws: Path) -> str:
    """Replace the workspace path with <WS> and the home directory with <HOME>."""
    ws = Path(ws)
    for base, tag in ((ws, "<WS>"), (Path.home(), "<HOME>")):
        forms = {str(base), base.as_posix()}
        try:
            r = base.resolve()
            forms |= {str(r), r.as_posix()}
        except OSError:
            pass
        for form in sorted(forms, key=len, reverse=True):
            if form:
                text = text.replace(form, tag)
    return text


def run_hidden_tests(task: Task, ws: Path) -> dict:
    """Copy hidden/ into ws and run test_cmd. Returns the raw run:
    {"junit": parse_junit dict, "exit": int|None, "timed_out": bool, "started": bool,
     "diag_tail": str}."""
    dest = Path(ws) / task.hidden_dest
    shutil.copytree(task.hidden_dir, dest, dirs_exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="pr-to-eval-junit-"))
    try:
        junit = tmp / "junit.xml"
        cmd = resolve_cmd(task.test_cmd, junit)
        try:
            res = run_process(cmd, cwd=ws, env=_env_for(Path(ws)), timeout=task.timeout_s)
        except OSError as e:
            return {"junit": parse_junit(junit), "exit": None, "timed_out": False,
                    "started": False, "diag_tail": scrub(f"runner could not start: {e}", ws)}
        diag = scrub(res.stdout + res.stderr, ws)[-4000:]
        return {"junit": parse_junit(junit), "exit": res.returncode,
                "timed_out": res.timed_out, "started": True, "diag_tail": diag}
    finally:
        remove_tree(tmp)


def run_checks(task: Task, ws: Path) -> dict:
    result = {"total": 0, "passed": 0, "failed": []}
    if not task.checks_path.exists():
        return result
    cmd = [find_bash(), task.checks_path.resolve().as_posix(),
           Path(ws).resolve().as_posix(), task.upstream_dir.resolve().as_posix()]
    res = run_process(cmd, cwd=ws, env=_env_for(Path(ws)), timeout=task.timeout_s)
    for line in res.stdout.splitlines():
        line = line.rstrip("\r")
        if line.startswith("PASS "):
            result["total"] += 1
            result["passed"] += 1
        elif line.startswith("FAIL "):
            result["total"] += 1
            name = line[5:].split(":", 1)[0].strip()
            result["failed"].append(f"check:{name}")
    if res.timed_out:
        result["total"] += 1
        result["failed"].append("check:checks.sh-timeout")
    elif res.returncode != 0 and not result["failed"]:
        result["total"] += 1
        result["failed"].append("check:checks.sh-error")
    return result


def empty_result(failed: list[str], diag: str = "") -> dict:
    return {"verdict": "FAIL",
            "tests": {"total": 0, "passed": 0, "failed": list(failed), "skipped": [], "missing": []},
            "checks": {"total": 0, "passed": 0, "failed": []},
            "runner": {"exit": None, "timed_out": False, "junit_ok": False, "diag_tail": diag}}


def judge_run(run: dict, expected: list[str] | None) -> tuple[str, dict]:
    """Turn a raw test run into (verdict, tests dict). The verdict here covers the test
    run only: "PASS", "FAIL" or "ERROR" (infrastructure, never a solution verdict)."""
    j = run["junit"]
    if run["timed_out"]:
        return "FAIL", {"total": 0, "passed": 0, "failed": ["<timeout>"], "skipped": [],
                        "missing": []}
    missing = sorted(set(expected or []) - set(j["ids"]))
    failed = (list(j["failed"]) + [f"skipped::{i}" for i in j["skipped"]]
              + [f"missing::{i}" for i in missing])
    tests = {"total": j["total"], "passed": j["passed"], "failed": failed,
             "skipped": list(j["skipped"]), "missing": missing}
    if not run["started"] or not j["ok"]:
        return "ERROR", tests
    if run["exit"] != 0 and not j["failed"] and not missing:
        return "ERROR", tests  # runner failed but the report shows nothing wrong
    if run["exit"] == 0 and j["failed"]:
        return "ERROR", tests  # report and exit code disagree
    if failed or j["total"] == 0 or run["exit"] != 0:
        return "FAIL", tests
    return "PASS", tests


def grade(task: Task, ws: Path, with_checks: bool = True,
          require_expected: bool = True) -> dict:
    """Grade a workspace. Shape:
    {"verdict": PASS|FAIL|ERROR, "tests": {total, passed, failed, skipped, missing},
     "checks": {...}, "runner": {exit, timed_out, junit_ok, diag_tail}}.
    with_checks=False skips checks.sh (mutation testing measures the hidden tests only).
    require_expected=False is for generating expected_tests.json from the reference."""
    run = run_hidden_tests(task, ws)
    expected = task.expected_tests()
    verdict, tests = judge_run(run, expected)
    if expected is None and require_expected:
        verdict = "ERROR"
        run["diag_tail"] = ("expected_tests.json is missing; run "
                            "`python -m harness.validate --write-expected`\n" + run["diag_tail"])
    checks = run_checks(task, ws) if with_checks else {"total": 0, "passed": 0, "failed": []}
    if verdict == "PASS" and checks["failed"]:
        verdict = "FAIL"
    return {"verdict": verdict, "tests": tests, "checks": checks,
            "runner": {"exit": run["exit"], "timed_out": run["timed_out"],
                       "junit_ok": run["junit"]["ok"], "diag_tail": run["diag_tail"]}}


def grade_patch(task: Task, patch: Path | None, keep: bool = False,
                root: Path | None = None) -> dict:
    """Fresh workspace + patch + grade. A patch that does not apply is a FAIL with
    an 'apply_error' field, never a crash. An empty patch is a FAIL (failed
    ["<empty-patch>"]) without running anything."""
    if patch is None or (Path(patch).exists() and patch_is_empty(Path(patch))):
        return empty_result(["<empty-patch>"])
    try:
        ws = make_workspace(task, patch, root=root)
    except ApplyError as e:
        res = empty_result([], diag=str(e))
        res["apply_error"] = str(e)
        return res
    try:
        return grade(task, ws)
    finally:
        if keep:
            print(f"kept workspace: {ws}", file=sys.stderr)
        else:
            remove_tree(ws)
