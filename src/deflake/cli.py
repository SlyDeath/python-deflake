"""Command-line entry point"""

from __future__ import annotations

import argparse
import contextlib
import io
import os
import re
import signal
import sys
import threading
import traceback
from collections.abc import Generator
from pathlib import Path
from typing import Any, TextIO, cast

from deflake import __version__, execution
from deflake.adapters import RUNNERS, Error
from deflake.check import Check
from deflake.reports import report

USAGE = "deflake TEST_PATH[:LINE|::NAME] [options]"
DESCRIPTION = (
    "Find flaky tests and explain why they fail. TEST_PATH may be a folder: every test_*.py, "
    "*_test.py and tests.py in it is checked."
)


def stream(given: TextIO | None, standard: TextIO | None) -> TextIO:
    """A standard stream closed from the start, as with `2>&-`, is None and writes go nowhere"""
    if given is not None:
        return given

    if standard is not None:
        return standard

    return Path(os.devnull).open("w")


def main(argv: list[str] | None = None, out: TextIO | None = None, err: TextIO | None = None) -> int:
    """Returns the exit code"""
    out = stream(out, sys.stdout)
    err = cast(TextIO, Messages(stream(err, sys.stderr), out))
    parser = build_parser()
    try:
        options = parser.parse_args(argv)
    except SystemExit as stop:
        return 0 if stop.code == 0 else 2

    try:
        with terminated_as_interrupt():
            return run(options, out, err)
    except Error as error:
        err.write(f"deflake: {error}\n")
        return 2
    except KeyboardInterrupt as stop:
        # A second interrupt while the tests tear down kills the test processes at once, and so do
        # SIGTERM and SIGHUP, which would otherwise end deflake and leave the test processes running
        with terminated_as_interrupt(), contextlib.suppress(KeyboardInterrupt):
            execution.stop_all()

        if isinstance(stop, Terminated):
            err.write("deflake: terminated\n")
            return 128 + stop.signal
        err.write("deflake: interrupted\n")
        return 130
    except Exception as error:
        # 1 would mean flaky tests were found, so anything unexpected is a 2 as well
        frames = "".join(traceback.format_tb(error.__traceback__)[-5:])
        err.write(f"deflake: unexpected error, please report it: {type(error).__name__}: {error}\n{frames}")
        return 2


class Messages(io.TextIOBase):
    """Messages to stderr, which a reader that stopped early, as `2>&1 | head` does, no longer takes"""

    def __init__(self, stream: TextIO, report: TextIO) -> None:
        self.stream = stream
        # Messages and the report go to one reader, so that reader is gone when messages fail
        self.shared = same_file(stream, report)
        self.gone = False

    def write(self, text: str) -> int:
        try:
            self.stream.write(text)
            self.stream.flush()
        except BrokenPipeError:
            self.gone = self.shared
            # Python would complain again when it flushes stderr on the way out
            stderr = self.stream is sys.stderr
            self.stream = Path(os.devnull).open("w")  # noqa: SIM115
            if stderr:
                sys.stderr = self.stream

        return len(text)


def same_file(first: TextIO, second: TextIO) -> bool:
    try:
        one, other = os.fstat(first.fileno()), os.fstat(second.fileno())
    except (OSError, ValueError, AttributeError):
        return False

    return (one.st_dev, one.st_ino) == (other.st_dev, other.st_ino)


class Terminated(KeyboardInterrupt):
    def __init__(self, number: int) -> None:
        super().__init__(number)
        self.signal = number


@contextlib.contextmanager
def terminated_as_interrupt() -> Generator[None, None, None]:
    """SIGTERM and SIGHUP, as from a closed terminal or a CI timeout, stop the test processes too:
    they run in sessions of their own, which these signals never reach"""
    if threading.current_thread() is not threading.main_thread():
        yield
        return

    numbers = [getattr(signal, name) for name in ("SIGTERM", "SIGHUP") if hasattr(signal, name)]
    # A signal ignored on purpose, as under nohup, stays ignored
    previous = {number: signal.getsignal(number) for number in numbers}
    previous = {number: handler for number, handler in previous.items() if handler is not signal.SIG_IGN}

    def stop(number: int, frame: Any) -> None:
        raise Terminated(number)

    for number in previous:
        signal.signal(number, stop)

    try:
        yield
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="deflake", usage=USAGE, description=DESCRIPTION)
    parser.add_argument("target", metavar="TEST_PATH", help="a test file, a test in it, or a folder")
    parser.add_argument(
        "-n",
        "--runs",
        type=positive,
        default=10,
        help="how many times to run the file (default: 10)",
    )
    parser.add_argument(
        "-j",
        "--jobs",
        type=positive,
        default=1,
        help="how many test processes to run at once (default: 1)",
    )
    parser.add_argument(
        "-t",
        "--timeout",
        type=positive,
        metavar="SECONDS",
        help="stop a test process that runs longer",
    )
    parser.add_argument(
        "-f",
        "--format",
        choices=("text", "json"),
        default="text",
        help="report format (default: text)",
    )
    parser.add_argument(
        "-r",
        "--runner",
        choices=RUNNERS,
        default="auto",
        help="the test runner (default: auto, pytest when the project configures it or the file uses it)",
    )
    parser.add_argument("-v", "--version", action="version", version=f"deflake {__version__}")

    return parser


def positive(text: str) -> int:
    try:
        number = int(text, 10)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text} is not a whole number") from None

    if number < 1:
        raise argparse.ArgumentTypeError(f"{text} (must be 1 or more)")

    return number


def run(options: argparse.Namespace, out: TextIO, err: TextIO) -> int:
    path, selector = target(options.target)
    execution.timeout = options.timeout
    execution.resume()
    check = Check(
        report(options.format, out, err),
        err=err,
        runs=options.runs,
        jobs=options.jobs,
        runner=options.runner,
    )
    if Path(path).is_file():
        return check.file(path, selector)
    if not Path(path).is_dir():
        raise Error(f"no such file: {path}")
    if selector:
        raise Error(f"{path} is a folder: a line or a test name needs a test file")

    return check.folder(path)


def target(argument: str) -> tuple[str, str | None]:
    """A file or folder whose name itself ends in ":12" is that one, not line 12 of another"""
    if Path(argument).exists():
        return argument, None

    if "::" in argument:
        path, name = argument.split("::", 1)
        return path, name

    match = re.fullmatch(r"(.+?):(\d+)", argument, re.DOTALL)

    return (match.group(1), match.group(2)) if match else (argument, None)
