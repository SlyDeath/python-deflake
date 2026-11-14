"""How to run a test file with each test runner: the command, its exit codes, how tests are named
and grouped, and the command that reproduces a run"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from typing import Any, Protocol

Row = dict[str, Any]


class Error(Exception):
    """A problem with what the user asked for, told to them as it is"""


class Adapter(Protocol):
    name: str

    def command(self, path: str, workdir: Path) -> list[str]:
        """The command that runs the file; workdir is a temporary folder of this run"""
        ...

    def success(self, code: int) -> bool:
        """Whether the exit code means the run itself went well"""
        ...

    def reproduce(self, path: str, seed: int) -> str: ...

    def group(self, id: str) -> str | None:
        """The group whose fixtures or hooks run with the test, such as its class"""
        ...

    def focus(self, path: str, selector: str, listed: list[Row]) -> list[str]:
        """Ids of the tests a LINE or ::NAME selector picks among the listed ones"""
        ...


RUNNERS = ("auto", "pytest", "django", "unittest")
# Files that configure pytest, and the section that does in files shared with other tools
PYTEST_CONFIG = {
    "pytest.toml": "",
    ".pytest.toml": "",
    "pytest.ini": "",
    ".pytest.ini": "",
    "pyproject.toml": "[tool.pytest",
    "tox.ini": "[pytest]",
    "setup.cfg": "[tool:pytest]",
}
# A test file that is pytest's, not unittest's: it imports pytest or has test functions of its own
PYTEST_FILE = re.compile(r"^(import pytest|from pytest\b|(async )?def test)", re.MULTILINE)


def for_path(path: str, runner: str = "auto") -> Adapter:
    from deflake.adapters.django import Django
    from deflake.adapters.pytest import Pytest
    from deflake.adapters.unittest import Unittest

    if runner == "auto":
        runner = detect(path)

    if runner == "pytest":
        if importlib.util.find_spec("pytest") is None:
            raise Error("pytest is not installed, install it or choose another runner with --runner")
        return Pytest()

    if runner == "django":
        if not Path("manage.py").is_file():
            raise Error("the django runner needs manage.py in the current folder")
        if importlib.util.find_spec("django") is None:
            raise Error("Django is not installed")
        return Django()

    if runner == "unittest":
        return Unittest()

    raise Error(f"unknown runner: {runner}")


def detect(path: str) -> str:
    """pytest for a project that configures it or a file written for it, then Django's runner for a
    project with manage.py, else unittest"""
    if pytest_project(Path(path)) or PYTEST_FILE.search(read(Path(path))):
        return "pytest"
    if Path("manage.py").is_file():
        return "django"

    return "unittest"


def pytest_project(path: Path) -> bool:
    """A pytest configuration in the current folder, or a conftest.py from the file up to it"""
    root = Path.cwd().resolve()
    for name, section in PYTEST_CONFIG.items():
        config = root / name
        if config.is_file() and section in read(config):
            return True

    folder = path.resolve().parent
    while folder.is_relative_to(root):
        if (folder / "conftest.py").is_file():
            return True
        if folder == root:
            break
        folder = folder.parent

    return False


def read(path: Path) -> str:
    try:
        return path.read_text(errors="replace")
    except OSError:
        return ""
