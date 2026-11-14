"""Runs a file with the project's manage.py test and deflake's runner on top of the project's own"""

from __future__ import annotations

import shlex
import sys
from pathlib import Path

from deflake.adapters import Row
from deflake.adapters.pytest import by_line
from deflake.adapters.unittest import Unittest, by_name

RUNNER = "deflake.django.Runner"


class Django(Unittest):
    name = "django"

    def command(self, path: str, workdir: Path) -> list[str]:
        return [sys.executable, "manage.py", "test", "--testrunner", RUNNER, "--noinput", label(path)]

    def success(self, code: int) -> bool:
        return code == 0

    def reproduce(self, path: str, seed: int) -> str:
        return f"DEFLAKE_SEED={seed} python manage.py test --testrunner {RUNNER} {shlex.quote(label(path))}"

    def focus(self, path: str, selector: str, listed: list[Row]) -> list[str]:
        if selector.isdigit():
            return by_line(path, int(selector), listed)

        return by_name(path, selector, listed)


def label(path: str) -> str:
    """The file, which deflake's runner turns into the module it is on the project's sys.path"""
    file = Path(path).resolve()
    cwd = Path.cwd().resolve()

    return str(file.relative_to(cwd)) if file.is_relative_to(cwd) else str(file)
