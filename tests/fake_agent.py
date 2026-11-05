"""Stand-in for the claude / codex CLIs in unit tests. Behaviour is chosen with a leading
`--fake-mode NAME` argument; the rest of argv is what the real CLI would receive."""
import json
import os
import subprocess
import sys
import time

argv = sys.argv[1:]
mode = "commit"
if argv[:1] == ["--fake-mode"]:
    mode, argv = argv[1], argv[2:]
if "--version" in argv:
    print("fake-cli 1.0")
    sys.exit(0)
codex = "exec" in argv
cwd = os.getcwd()


def emit(obj):
    print(json.dumps(obj), flush=True)


def claude_tool(name, **inp):
    emit({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "t1", "name": name, "input": inp}]}})


def codex_cmd(command):
    emit({"type": "item.completed", "item": {"id": "i1", "type": "command_execution",
                                             "command": command, "aggregated_output": "",
                                             "exit_code": 0, "status": "completed"}})


if codex:
    emit({"type": "thread.started", "thread_id": "x"})
else:
    emit({"type": "system", "subtype": "init", "model": "fake-model-1", "cwd": cwd})

if mode == "hang":
    time.sleep(120)
elif mode == "nochange":
    pass
elif mode == "escape":
    outside = os.path.join(os.path.dirname(os.path.dirname(cwd)), "secret.txt")
    if codex:
        codex_cmd(f'powershell -Command "cat {outside}"')
    else:
        claude_tool("Read", file_path=outside)
elif mode == "commit":
    path = os.path.join(cwd, "tiny", "mathx.py")
    src = open(path, encoding="utf-8").read()
    src = src.replace("    return value\n", "    if value > high:\n        return high\n    return value\n")
    open(path, "w", encoding="utf-8", newline="\n").write(src)
    subprocess.run(["git", "add", "-A"], cwd=cwd, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "fix clamp"], cwd=cwd, check=True)
    with open(os.path.join(cwd, "notes.txt"), "w", encoding="utf-8", newline="\n") as f:
        f.write("uncommitted\n")  # a change that was never committed
    open(os.path.join(cwd, "tiny", "x.pyc"), "wb").write(b"\0")  # ignored by .gitignore
    if codex:
        codex_cmd("python -m pytest -q")
    else:
        claude_tool("Bash", command="python -m pytest -q")
emit({"type": "result", "subtype": "success"})
