import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

FIXTURES = Path(__file__).parent.parent / "fixtures"


@dataclass
class Result:
    code: int
    out: str
    err: str

    @property
    def json(self) -> Any:
        return json.loads(self.out)


Deflake = Callable[..., Result]


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    # The hook sees the items of the whole session, not only those of this folder
    here = Path(__file__).parent
    for item in items:
        if item.path.is_relative_to(here):
            item.add_marker(pytest.mark.e2e)


@pytest.fixture
def project(tmp_path: Path) -> Callable[[str], Path]:
    """Copies a fixture project to a folder of its own, so its rootdir is not this repository's"""

    def copy(name: str) -> Path:
        target = tmp_path / name
        shutil.copytree(FIXTURES / name, target)

        return target

    return copy


@pytest.fixture
def deflake() -> Deflake:
    def run(*args: str, cwd: Path, env: dict[str, str] | None = None, timeout: int = 240) -> Result:
        process = subprocess.run(
            [sys.executable, "-m", "deflake", *args],
            cwd=cwd,
            env={**os.environ, **(env or {})},
            capture_output=True,
            text=True,
            timeout=timeout,
        )

        return Result(process.returncode, process.stdout, process.stderr)

    return run
