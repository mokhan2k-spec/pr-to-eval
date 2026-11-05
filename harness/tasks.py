"""Load and validate task.json / solutions.json."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TASKS_DIR = ROOT / "tasks"
RUNS_DIR = ROOT / "runs"
WORK_DIR = ROOT / ".work"
CORPUS_PATH = ROOT / "corpus.json"

KINDS = ("reference", "alt", "flawed")
KIND_ORDER = {k: i for i, k in enumerate(KINDS)}
REQUIRED = ("id", "language", "title", "upstream", "source_files",
            "hidden_dest", "test_cmd", "timeout_s")
UPSTREAM_REQUIRED = ("repo", "pr", "pr_url", "base_sha", "merge_sha",
                     "license", "vendored_files")


class TaskError(ValueError):
    """A task directory does not follow the contract."""


def utf8_stdout() -> None:
    """Tables contain non-ASCII characters; Windows consoles default to cp1252."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise TaskError(f"{path}: file not found") from None
    except json.JSONDecodeError as e:
        raise TaskError(f"{path}: invalid JSON: {e}") from None


def validate_task_data(data: dict) -> list[str]:
    problems = []
    for key in REQUIRED:
        if key not in data:
            problems.append(f"task.json: missing '{key}'")
    up = data.get("upstream")
    if isinstance(up, dict):
        for key in UPSTREAM_REQUIRED:
            if key not in up:
                problems.append(f"task.json: upstream missing '{key}'")
    elif "upstream" in data:
        problems.append("task.json: 'upstream' must be an object")
    if data.get("language") not in ("python", "typescript"):
        problems.append("task.json: language must be 'python' or 'typescript'")
    if not isinstance(data.get("test_cmd", []), list) or not data.get("test_cmd"):
        problems.append("task.json: test_cmd must be a non-empty list")
    return problems


def validate_solutions_data(data: dict) -> list[str]:
    problems = []
    if not isinstance(data, dict) or "reference" not in data:
        return ["solutions.json: must be an object with a 'reference' entry"]
    for name, spec in data.items():
        kind = spec.get("kind")
        if kind not in KINDS:
            problems.append(f"solutions.json: {name}: kind must be one of {KINDS}")
        want = "fail" if kind == "flawed" else "pass"
        if spec.get("expect") != want:
            problems.append(f"solutions.json: {name}: expect must be '{want}'")
        if kind == "flawed" and not spec.get("expect_failing"):
            problems.append(f"solutions.json: {name}: flawed needs non-empty expect_failing")
    return problems


class Task:
    def __init__(self, path: Path, data: dict):
        self.path = Path(path)
        self.data = data

    # --- task.json fields -------------------------------------------------
    id = property(lambda s: s.data["id"])
    language = property(lambda s: s.data["language"])
    title = property(lambda s: s.data["title"])
    upstream = property(lambda s: s.data["upstream"])
    source_files = property(lambda s: s.data["source_files"])
    hidden_dest = property(lambda s: s.data["hidden_dest"])
    test_cmd = property(lambda s: list(s.data["test_cmd"]))
    timeout_s = property(lambda s: s.data["timeout_s"])
    mutation = property(lambda s: s.data.get("mutation") or {})

    # --- layout -----------------------------------------------------------
    upstream_dir = property(lambda s: s.path / "upstream")
    hidden_dir = property(lambda s: s.path / "hidden")
    checks_path = property(lambda s: s.path / "checks.sh")
    solutions_dir = property(lambda s: s.path / "solutions")

    def task_md(self) -> str:
        return (self.path / "TASK.md").read_text(encoding="utf-8")

    def grader_prompt(self) -> str:
        return (self.path / "grader_prompt.md").read_text(encoding="utf-8")

    def solutions(self) -> dict:
        data = _read_json(self.solutions_dir / "solutions.json")
        problems = validate_solutions_data(data)
        if problems:
            raise TaskError(f"{self.id}: " + "; ".join(problems))
        return data

    def sorted_solutions(self) -> list[tuple[str, dict]]:
        items = self.solutions().items()
        return sorted(items, key=lambda kv: (KIND_ORDER.get(kv[1]["kind"], 9), kv[0]))

    def patch_path(self, name: str) -> Path:
        return self.solutions_dir / f"{name}.patch"

    @property
    def expected_tests_path(self) -> Path:
        return self.path / "expected_tests.json"

    def expected_tests(self) -> list[str] | None:
        """Sorted test ids of the reference run, or None if the file does not exist."""
        if not self.expected_tests_path.exists():
            return None
        data = _read_json(self.expected_tests_path)
        if not isinstance(data, list) or not all(isinstance(i, str) for i in data):
            raise TaskError(f"{self.id}: expected_tests.json must be a list of strings")
        return data

    def equivalent_mutants(self) -> dict:
        path = self.path / "equivalent_mutants.json"
        return _read_json(path) if path.exists() else {}

    def llm_grade(self, solution: str):
        path = self.path / "llm_grades" / f"{solution}.json"
        if not path.exists():
            return None
        return _read_json(path)


def load_task(path: Path) -> Task:
    path = Path(path)
    data = _read_json(path / "task.json")
    problems = validate_task_data(data)
    if problems:
        raise TaskError(f"{path.name}: " + "; ".join(problems))
    if data["id"] != path.name:
        raise TaskError(f"{path.name}: task.json id '{data['id']}' != directory name")
    return Task(path, data)


def is_production_dir(tasks_dir: Path) -> bool:
    return Path(tasks_dir).resolve() == TASKS_DIR.resolve()


def load_corpus(path: Path | None = None) -> dict:
    return _read_json(Path(path) if path else CORPUS_PATH)


def load_tasks(tasks_dir: Path | None = None, only: str | None = None,
               strict: bool | None = None) -> list[Task]:
    """Load every task under tasks_dir. For the production tasks/ dir (strict, the
    default there) a sub-directory without task.json is an error, not skipped."""
    tasks_dir = Path(tasks_dir) if tasks_dir else TASKS_DIR
    if not tasks_dir.is_dir():
        return []
    if strict is None:
        strict = is_production_dir(tasks_dir)
    if strict:
        stray = sorted(p.name for p in tasks_dir.iterdir()
                       if p.is_dir() and not (p / "task.json").exists())
        if stray:
            raise TaskError(f"directories under {tasks_dir.name}/ without task.json: {stray}")
    dirs = sorted(p for p in tasks_dir.iterdir() if (p / "task.json").exists())
    if only is not None:
        dirs = [p for p in dirs if p.name == only]
        if not dirs:
            raise TaskError(f"no task '{only}' in {tasks_dir}")
    return [load_task(p) for p in dirs]
