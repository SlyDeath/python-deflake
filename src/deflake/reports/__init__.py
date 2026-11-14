"""Reports of the diagnoses: text for people, JSON for CI and other tools"""

from __future__ import annotations

from typing import Protocol, TextIO

from deflake.models import Diagnosis
from deflake.reports.json import JsonReport
from deflake.reports.text import TextReport


class Report(Protocol):
    def start(self, file: str, number: int, total: int) -> None:
        """Heads the tests of each file when a folder is checked"""
        ...

    def not_checked(self, file: str, message: str) -> None: ...

    def print(
        self,
        diagnoses: list[Diagnosis],
        *,
        file: str,
        not_run: list[str],
        shared: list[str],
    ) -> None: ...

    def finish(self) -> None: ...

    def flush(self) -> None: ...


def report(format: str, out: TextIO, err: TextIO) -> Report:
    return JsonReport(out, err) if format == "json" else TextReport(out, err)


__all__ = ["JsonReport", "Report", "TextReport", "report"]
