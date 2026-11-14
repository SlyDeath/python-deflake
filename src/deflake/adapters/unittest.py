"""Runs a file with deflake's own unittest runner"""

from __future__ import annotations

import shlex
import sys
from pathlib import Path

from deflake.adapters import Error, Row
from deflake.adapters.pytest import by_line

RUNNER = "deflake.unittest_runner"


class Unittest:
    name = "unittest"

    def command(self, path: str, workdir: Path) -> list[str]:
        return [sys.executable, "-m", RUNNER, path]

    def success(self, code: int) -> bool:
        return code in (0, 5)

    def reproduce(self, path: str, seed: int) -> str:
        return f"DEFLAKE_SEED={seed} python -m {RUNNER} {shlex.quote(path)}"

    def group(self, id: str) -> str | None:
        """module.Class.test_name belongs to module.Class"""
        return id.rsplit(".", 1)[0] if id.count(".") >= 2 else None

    def focus(self, path: str, selector: str, listed: list[Row]) -> list[str]:
        if selector.isdigit():
            return by_line(path, int(selector), listed)

        return by_name(path, selector, listed)


def by_name(path: str, name: str, rows: list[Row]) -> list[str]:
    """Class.test_name, a test name of any class, or every test of a class"""
    chosen = [
        row["listed"]
        for row in rows
        if row["name"] == name or row["name"].endswith(f".{name}") or row["name"].startswith(f"{name}.")
    ]
    if not chosen:
        raise Error(f"no test named {name} in {path}")

    return chosen
