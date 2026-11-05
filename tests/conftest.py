"""Fixture task directories hold hidden tests that must not be collected here."""
import tempfile
from pathlib import Path

import pytest

collect_ignore_glob = ["fixtures/*"]
FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES


@pytest.fixture
def task(fixtures_dir):
    from harness.tasks import load_task
    return load_task(fixtures_dir / "tiny-python-1")


@pytest.fixture
def tmp_path():
    """Own tmp dir: pytest's default basetemp is unusable on some locked-down machines."""
    from harness.workspace import remove_tree
    path = Path(tempfile.mkdtemp(prefix="pr-to-eval-test-"))
    yield path
    remove_tree(path)


def good_probe_record(agent: str, cli_version: str) -> dict:
    """A boundary-probe record that passes verdict_ok and is bound to the CURRENT policy."""
    from harness import agent_runs
    from harness import boundary_probe as bp
    rec = {r: {"result": "denied"} for r in bp.REQUIRED_DENIED[agent]}
    rec.update({r: {"result": "denied"} for r in bp.REQUIRED_DECIDED[agent]})
    for r in bp.REQUIRED_ALLOWED:
        rec[r] = {"result": "allowed"}
    rec["_binding"] = {"cli_version": cli_version, "policy_digest": agent_runs.policy_digest(agent),
                       "probe_digest": agent_runs.probe_digest()}
    rec["_ok"] = True
    rec["_problems"] = []
    return rec


@pytest.fixture
def probe_record():
    return good_probe_record
