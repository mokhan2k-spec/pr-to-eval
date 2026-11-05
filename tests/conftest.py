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
