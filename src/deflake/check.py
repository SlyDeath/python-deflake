"""Investigates a test file, or every test file under a folder, and prints the report.

Returns the exit code: 0 when nothing was found, 1 when something was, 2 when tests could not run
"""

from __future__ import annotations

import contextlib
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import TextIO

from deflake.adapters import Error, for_path
from deflake.investigation import Investigation
from deflake.models import Diagnosis
from deflake.reports import Report

PATTERNS = ("test_*.py", "*_test.py", "tests.py")
# Folders of installed packages and tools, whose tests are not the project's
SKIPPED = {".venv", "venv", ".tox", ".nox", "site-packages", "node_modules", "__pycache__", ".git"}


class Closed(Exception):
    """The reader is gone before a file was checked"""


# The exit code of a program SIGPIPE stops
CLOSED = 141


class Check:
    def __init__(self, report: Report, *, err: TextIO, runs: int, jobs: int, runner: str = "auto") -> None:
        self.report = report
        self.err = err
        self.runs = runs
        self.jobs = jobs
        self.runner = runner
        self.folder_mode = False
        self.closed = False

    def file(self, path: str, selector: str | None) -> int:
        return self.finished(lambda: self.investigate(path, selector))

    def folder(self, path: str) -> int:
        """The files run one after another. A file that cannot run does not stop the others"""
        files = test_files(path)
        if not files:
            raise Error(f"no test files ({', '.join(PATTERNS)}) in {path}")

        self.folder_mode = True
        codes: dict[str, int] = {}
        try:
            self.finished(lambda: self.check_each(files, codes))
        finally:
            if codes:
                self.summarize(len(files), codes)

        if self.closed and len(codes) < len(files):
            # The files left are not checked: not a success, as with a process a closed pipe stops
            return CLOSED

        return max(codes.values())

    def check_each(self, files: list[str], codes: dict[str, int]) -> int:
        """Stops when the reader of the report is gone. After an interrupt the files checked so far
        are still reported"""
        try:
            for number, file in enumerate(files, 1):
                if self.closed:
                    break
                with contextlib.suppress(Closed):
                    codes[file] = self.investigate_in_folder(file, number, len(files))
        except KeyboardInterrupt:
            self.output(self.report.finish)
            raise

        return 0

    def finished(self, work: Callable[[], int]) -> int:
        result = work()
        self.output(self.report.finish)

        return result

    def investigate_in_folder(self, file: str, number: int, total: int) -> int:
        self.output(lambda: self.report.start(file, number, total))
        if self.closed:
            raise Closed

        try:
            return self.investigate(file, None)
        except Error as error:
            message = str(error)

        self.err.write(f"deflake: {message}\n")
        self.output(lambda: self.report.not_checked(file, message))

        return 2

    def investigate(self, path: str, selector: str | None) -> int:
        investigation = Investigation(
            for_path(path, self.runner),
            path,
            runs=self.runs,
            selector=selector,
            jobs=self.jobs,
        )
        diagnoses = investigation()

        return self.print(path, diagnoses, selector, investigation)

    def print(
        self,
        path: str,
        diagnoses: list[Diagnosis],
        selector: str | None,
        investigation: Investigation,
    ) -> int:
        """A selected test that never started is an error: something stopped every run before it,
        or the project's own filters leave it out"""
        not_run = investigation.not_run
        if not_run:
            self.err.write(
                f"deflake: {', '.join(not_run)} never ran in a run of the whole file "
                "(a filter of the project leaves it out, or something stops every run before it)\n"
            )

        if not diagnoses and not not_run:
            self.err.write(f"deflake: {self.skipped(path, selector)}, nothing to report\n")

        self.output(
            lambda: self.report.print(
                diagnoses,
                file=path,
                not_run=not_run,
                shared=investigation.shared_names,
            )
        )

        return 0 if all(diagnosis.clean for diagnosis in diagnoses) and not not_run else 1

    def output(self, write: Callable[[], None]) -> None:
        """A reader that stops early, such as head, is not an error of deflake's"""
        if self.closed:
            return

        try:
            write()
            self.report.flush()
            if getattr(self.err, "gone", False):
                raise BrokenPipeError
        except BrokenPipeError:
            self.closed = True
            # Python would complain again when it flushes stdout on the way out
            sys.stdout = Path(os.devnull).open("w")  # noqa: SIM115

    def skipped(self, path: str, selector: str | None) -> str:
        if selector:
            return f"the tests at {selector} skipped themselves in every run"

        return f"every test of {path} skipped itself" if self.folder_mode else "every test skipped itself"

    def summarize(self, total: int, codes: dict[str, int]) -> None:
        checked = str(total) if len(codes) == total else f"{len(codes)} of {total}"
        nothing = ", nothing found" if set(codes.values()) == {0} else ""
        self.err.write(f"deflake: checked {checked} files{nothing}\n")
        for code, title in ((1, "failing or leaking tests in:"), (2, "could not check:")):
            files = [file for file, value in codes.items() if value == code]
            if files:
                self.err.write(title + "\n" + "".join(f"  {file}\n" for file in files))


def test_files(path: str) -> list[str]:
    root = Path(path)
    found = {
        file
        for pattern in PATTERNS
        for file in root.rglob(pattern)
        if file.is_file() and not SKIPPED & set(file.relative_to(root).parts[:-1])
    }

    return sorted(str(file) for file in found)
