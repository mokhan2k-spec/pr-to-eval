"""Replace / check the generated blocks between README markers."""
from __future__ import annotations

import re
from pathlib import Path

from .tasks import ROOT

README = ROOT / "README.md"
VALIDATION = "validation-table"
MUTATION = "mutation-table"


class ReadmeError(Exception):
    pass


def _markers(name: str) -> tuple[str, str]:
    return f"<!-- {name}:start -->", f"<!-- {name}:end -->"


def _normalise(text: str) -> str:
    return text.replace("\r\n", "\n")


def _block(content: str) -> str:
    return "\n" + content.strip("\n") + "\n"


def extract_block(text: str, name: str) -> str:
    start, end = _markers(name)
    m = re.search(re.escape(start) + r"(.*?)" + re.escape(end), _normalise(text), re.S)
    if not m:
        raise ReadmeError(f"README markers for '{name}' not found")
    return m.group(1)


def replace_block(text: str, name: str, content: str) -> str:
    start, end = _markers(name)
    text = _normalise(text)
    extract_block(text, name)  # raises if missing
    pattern = re.compile(re.escape(start) + r".*?" + re.escape(end), re.S)
    return pattern.sub(lambda _m: start + _block(content) + end, text, count=1)


def write_block(name: str, content: str, path: Path = README) -> None:
    path = Path(path)
    new = replace_block(path.read_text(encoding="utf-8"), name, content)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(new)


def check_block(name: str, content: str, path: Path = README) -> bool:
    current = extract_block(Path(path).read_text(encoding="utf-8"), name)
    return current == _block(content)
