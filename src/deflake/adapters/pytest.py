"""Runs a file with pytest and deflake's plugin"""

from __future__ import annotations

import shlex
import sys
from pathlib import Path

from deflake.adapters import Error, Row

PLUGIN = "deflake.pytest_plugin"


class Pytest:
    name = "pytest"

    def command(self, path: str, workdir: Path) -> list[str]:
        # The cache goes to the run's own folder: the project's --lf would pick up deflake's failures
        return [sys.executable, "-m", "pytest", "-p", PLUGIN, "-o", f"cache_dir={workdir / 'cache'}", path]

    def success(self, code: int) -> bool:
        return code in (0, 5)

    def reproduce(self, path: str, seed: int) -> str:
        return f"DEFLAKE_SEED={seed} python -m pytest -p {PLUGIN} {shlex.quote(path)}"

    def group(self, id: str) -> str | None:
        file, separator, name = id.partition("::")
        if not separator:
            return None

        # Parameter labels may contain :: and brackets of their own
        parent, separator, _ = name.partition("[")[0].rpartition("::")

        return f"{file}::{parent}" if separator else file

    def focus(self, path: str, selector: str, listed: list[Row]) -> list[str]:
        here = Path(path).resolve()
        rows = [row for row in listed if Path(row["file"]).resolve() == here]
        if selector.isdigit():
            return by_line(path, int(selector), rows)

        return by_name(path, selector, rows)


def by_line(path: str, line: int, rows: list[Row]) -> list[str]:
    """The tests whose body holds the line, else every test of the innermost class that does, else
    the tests declared closest above it. Only lines of tests and classes in the file count: a class
    imported from another module has its lines there"""
    here = Path(path).resolve()
    rows = [row for row in rows if row["file"] and Path(row["file"]).resolve() == here]
    inside = [row["listed"] for row in rows if row["line"] <= line <= row["end"]]
    if inside:
        return inside

    spans = {tuple(span) for row in rows for span in row["groups"] if span[0] <= line <= span[1]}
    if spans:
        innermost = max(spans, key=lambda span: span[0])
        return [row["listed"] for row in rows if innermost in map(tuple, row["groups"])]

    above = [row["line"] for row in rows if 0 < row["line"] <= line]
    if not above:
        raise Error(f"no test found at {path}:{line}")

    nearest = max(above)

    return [row["listed"] for row in rows if row["line"] == nearest]


def by_name(path: str, name: str, rows: list[Row]) -> list[str]:
    """The test with this name, all its parameters, or every test of a class with this name"""
    chosen = [
        row["listed"]
        for row in rows
        if row["name"] == name or row["name"].startswith((f"{name}[", f"{name}::"))
    ]
    if not chosen:
        raise Error(f"no test named {name} in {path}")

    return chosen
