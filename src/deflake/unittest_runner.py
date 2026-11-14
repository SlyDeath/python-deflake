"""Runs a unittest file for deflake: python -m deflake.unittest_runner FILE.

Without deflake around it, it runs the file as unittest does, in the order of DEFLAKE_SEED when set,
as the reproduce command does
"""

from __future__ import annotations

import contextlib
import importlib
import inspect
import re
import sys
import traceback
import unittest
from collections.abc import Generator, Iterator
from pathlib import Path
from types import TracebackType
from typing import Any

from deflake import child, leaks

ExcInfo = tuple[type[BaseException], BaseException, TracebackType] | tuple[None, None, None]

# Exit codes as pytest has them
OK, FAILED, USAGE, NO_TESTS = 0, 1, 4, 5


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        sys.stderr.write("usage: python -m deflake.unittest_runner TEST_FILE\n")
        return USAGE

    request = child.request()
    try:
        module = importlib.import_module(module_name(args[0]))
    except Exception as error:
        # As pytest reports a file it cannot collect: an error outside of tests in every run
        text = import_error(error)
        sys.stderr.write(f"error importing {args[0]}:\n{text}\n")
        if request is not None and request.output:
            child.Recorder(request.output, request.seed, request.probe).outside(
                f"error importing {args[0]}:\n{text}"
            )
        return FAILED

    return run(unittest.defaultTestLoader.loadTestsFromModule(module), request)


def import_error(error: Exception) -> str:
    """The traceback without the frames of importlib and of this runner"""
    frames = [
        frame
        for frame in traceback.extract_tb(error.__traceback__)
        if not frame.filename.startswith("<frozen")
        and Path(frame.filename) not in (Path(__file__), Path(importlib.__file__))
    ]
    lines = traceback.format_list(frames) + traceback.format_exception_only(type(error), error)

    return "".join(lines).rstrip()


def module_name(path: str) -> str:
    """As python -m unittest takes a file: the module of that name, from the current folder"""
    file = Path(path).resolve()
    try:
        relative = file.relative_to(Path.cwd().resolve())
    except ValueError:
        raise SystemExit(f"deflake: {path} is outside of the current folder") from None

    return ".".join(relative.with_suffix("").parts)


def run(suite: unittest.TestSuite, request: child.Request | None) -> int:
    tests = list(flatten(suite))
    if request is None:
        request = child.Request(output=None, seed=None)

    recorder = child.Recorder(request.output, request.seed, request.probe) if request.output else None
    if request.listing:
        if recorder is not None:
            for test in tests:
                list_test(recorder, test)
        return OK

    chosen = child.arrange(tests, request, lambda test: test.id(), groups)
    if not chosen:
        return NO_TESTS

    watch = child.Watch() if recorder else None
    runner = unittest.TextTestRunner(resultclass=result_class(recorder, request.seed, chosen))
    with watching(unittest.TestCase):
        result = runner.run(unittest.TestSuite(chosen))

    report_outside(recorder, watch, result)

    return OK if result.wasSuccessful() else FAILED


@contextlib.contextmanager
def watching(*classes: type[unittest.TestCase]) -> Generator[None, None, None]:
    """A test's leaks are what is left after the whole call of the test: an IsolatedAsyncioTestCase
    closes its event loop, and a Django test rolls back its transaction, after stopTest"""
    originals = {cls: cls.__dict__["__call__"] for cls in classes}
    for cls, call in originals.items():
        cls.__call__ = watched(call)  # type: ignore[method-assign]

    try:
        yield
    finally:
        for cls, call in originals.items():
            cls.__call__ = call  # type: ignore[method-assign]


def watched(call: Any) -> Any:
    def __call__(test: unittest.TestCase, *args: Any, **kwargs: Any) -> Any:
        result = args[0] if args else kwargs.get("result")
        # The outermost call watches: a Django test's call runs unittest's inside it
        if not isinstance(result, Recording) or result.recorder is None or result.calling is not None:
            return call(test, *args, **kwargs)

        result.calling = test
        # A skipped test does not use the setup its class did early: the next test does
        early = None if skips(test) else result.eager.pop(type(test), None)
        if early is not None:
            before, rest = early
            before.excused = rest.leaked()
            result.outer = before
        else:
            result.outer = child.Watch()

        try:
            return call(test, *args, **kwargs)
        finally:
            result.finish_outer()

    return __call__


def skips(test: unittest.TestCase) -> bool:
    method = getattr(test, getattr(test, "_testMethodName", ""), None)

    return bool(
        getattr(type(test), "__unittest_skip__", False) or getattr(method, "__unittest_skip__", False)
    )


def report_outside(recorder: child.Recorder | None, watch: child.Watch | None, result: Any) -> None:
    """What setUpClass, setUpModule and their teardowns left behind beyond the leaks of the tests"""
    if recorder is None or watch is None or not isinstance(result, Recording):
        return

    own = leaks.own(watch.leaked(), result.reported)
    if own:
        label = "class or module setup (setUpClass, setUpModule), outside its tests"
        recorder.outside(None, [f"{label}: {leak}" for leak in own])


def flatten(suite: unittest.TestSuite | unittest.TestCase) -> Iterator[unittest.TestCase]:
    """Tests in a suite, or a test that failed to load: unittest makes one that reports why"""
    if isinstance(suite, unittest.TestSuite):
        for member in suite:
            yield from flatten(member)
    else:
        yield suite


def groups(test: unittest.TestCase) -> list[str]:
    """The module and class of the test, so their setUpModule and setUpClass run once"""
    cls = type(test)

    return [cls.__module__, f"{cls.__module__}.{cls.__qualname__}"]


def name_of(test: unittest.TestCase) -> str:
    """Class.test_name, the id without the module"""
    module = type(test).__module__

    return test.id().removeprefix(f"{module}.")


def list_test(recorder: child.Recorder, test: unittest.TestCase) -> None:
    cls = type(test)
    method = getattr(cls, getattr(test, "_testMethodName", ""), None)
    file = inspect.getsourcefile(cls) or ""
    # A test of a mixin from another file has no line in this one
    line, end = lines_of(method) if source_file(method) in (file, None) else (0, 0)
    recorder.listed(test.id(), name_of(test), file, line, end, [list(lines_of(cls))])


def source_file(obj: Any) -> str | None:
    try:
        return inspect.getsourcefile(inspect.unwrap(obj))
    except TypeError:
        return None


def lines_of(obj: Any) -> tuple[int, int]:
    try:
        source, first = inspect.getsourcelines(inspect.unwrap(obj))
    except (TypeError, OSError):
        return 0, 0

    return first, first + len(source) - 1


def result_class(
    recorder: child.Recorder | None,
    seed: int | None,
    planned: list[unittest.TestCase],
    base: type[unittest.TestResult] = unittest.TextTestResult,
) -> type[Recording]:
    """Records on top of the result of the project's runner, which its runner may rely on"""
    bases = (Recording,) if issubclass(Recording, base) else (Recording, base)

    class Result(*bases):  # type: ignore[misc]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            self.recorder = recorder
            self.seed = seed
            self.planned = planned

    return Result


class Recording(unittest.TextTestResult):
    """Records every test with what it leaked, setUp and tearDown included"""

    recorder: child.Recorder | None
    seed: int | None
    # The tests to run, to tell which ones a skipped setUpClass or setUpModule skips
    planned: list[unittest.TestCase]
    # The result of the tests that run now, for setup that runs outside of a test (see deflake.django)
    active: Recording | None = None

    def __init__(self, stream: Any, descriptions: bool, verbosity: int, *args: Any, **kwargs: Any) -> None:
        if type(self).__mro__[type(self).__mro__.index(Recording) + 1] is unittest.TextTestResult:
            # A project's runner may pass more, as xmlrunner's elapsed times: unittest's result takes
            # none of them but durations, from Python 3.12
            known = {key: value for key, value in kwargs.items() if key == "durations"}
            super().__init__(stream, descriptions, verbosity, **known)
        else:
            super().__init__(stream, descriptions, verbosity, *args, **kwargs)

        Recording.active = self
        # The test that runs now, and the state before it
        self.current: unittest.TestCase | None = None
        self.watch: child.Watch | None = None
        # The test whose call runs now, and the state before that call (see watched)
        self.calling: unittest.TestCase | None = None
        self.outer: child.Watch | None = None
        # The state before the setup of a class that sets up its first test early, by the class
        # with the state after it, as the rest of the setup of the class is not the test's
        self.eager: dict[type, tuple[child.Watch, child.Watch]] = {}
        self.error: str | None = None
        self.skipped_now = False
        self.expected_failure = False
        # Every leak the tests reported so far
        self.reported: list[str] = []

    def startTest(self, test: unittest.TestCase) -> None:
        super().startTest(test)
        child.reseed(self.seed)
        self.begin(test)

    def begin(self, test: unittest.TestCase) -> None:
        self.error = None
        self.skipped_now = False
        self.expected_failure = False
        # A module that failed to import is not a test: its error is outside of tests
        if self.recorder is not None and not failed_import(test) and self.current is not test:
            self.recorder.started(test.id(), name_of(test))
            self.current = test
            self.watch = self.outer or child.Watch()

    def stopTest(self, test: unittest.TestCase) -> None:
        super().stopTest(test)
        if self.outer is None:
            self.finish()

    def finish_outer(self) -> None:
        self.finish()
        self.outer = None
        self.calling = None

    def finish(self) -> None:
        test, watch = self.current, self.watch
        if self.recorder is None or test is None or watch is None:
            return

        self.current = self.watch = None
        leaked = watch.leaked()
        self.reported.extend(leaked)
        if self.error is not None:
            self.recorder.finished(test.id(), name_of(test), passed=False, error=self.error, leaked=leaked)
        else:
            skipped = self.skipped_now
            self.recorder.finished(
                test.id(),
                name_of(test),
                passed=True,
                skipped=skipped,
                expected_failure=self.expected_failure,
                leaked=leaked,
            )

    def addError(self, test: unittest.TestCase, err: ExcInfo) -> None:
        super().addError(test, err)
        self.failed(test, err)

    def addFailure(self, test: unittest.TestCase, err: ExcInfo) -> None:
        super().addFailure(test, err)
        self.failed(test, err)

    def addSubTest(self, test: unittest.TestCase, subtest: unittest.TestCase, err: ExcInfo | None) -> None:
        super().addSubTest(test, subtest, err)
        if err is not None and err[0] is not None:
            self.failed(test, err, f"{subtest._subDescription()}: ")  # type: ignore[attr-defined]

    def addSkip(self, test: unittest.TestCase, reason: str) -> None:
        super().addSkip(test, reason)
        self.skipped_now = True
        if self.current is None and type(test).__name__ == "_ErrorHolder":
            self.skip_planned(str(test))

    def skip_planned(self, description: str) -> None:
        """setUpClass or setUpModule skipped all their tests, which never start: they skipped
        themselves, as in pytest"""
        match = re.fullmatch(r"setUp(Class|Module) \((.+)\)", description)
        if self.recorder is None or match is None:
            return

        for test in self.planned:
            if match[2] in groups(test)[-1 if match[1] == "Class" else 0 :]:
                self.recorder.started(test.id(), name_of(test))
                self.recorder.finished(test.id(), name_of(test), passed=True, skipped=True)

    def addExpectedFailure(self, test: unittest.TestCase, err: ExcInfo) -> None:
        super().addExpectedFailure(test, err)
        self.expected_failure = True

    def addUnexpectedSuccess(self, test: unittest.TestCase) -> None:
        super().addUnexpectedSuccess(test)
        if self.current is not None:
            self.error = self.error or "passed, but is marked as an expected failure"

    def failed(self, test: unittest.TestCase, err: ExcInfo, prefix: str = "") -> None:
        if self.recorder is None:
            return

        error = prefix + message(err)
        if self.current is None and self.calling is test:
            # The test failed to set up before it started, as a Django test that cannot load its
            # fixtures does
            self.begin(test)
            error = f"error in setup: {error}"

        if self.current is None:
            # Not a test that runs now: setUpClass, setUpModule or their teardown failed
            self.recorder.outside(f"error in {test}:\n{error}")
        elif self.error is None:
            self.error = error


def failed_import(test: unittest.TestCase) -> bool:
    return type(test).__module__ == "unittest.loader"


def message(err: ExcInfo) -> str:
    kind, value, tb = err
    if kind is None:
        return "unknown error"

    text = "".join(traceback.format_exception_only(kind, value)).strip()
    frames = [frame for frame in traceback.extract_tb(tb) if not own_frame(frame.filename)]
    if frames:
        return f"{text}\n  at {Path(frames[-1].filename).name}:{frames[-1].lineno}"

    return text


def own_frame(filename: str) -> bool:
    """Frames of unittest and of Django's test case say nothing about the test"""
    path = Path(filename)

    return path.parent == Path(unittest.__file__).parent or path.parent.parts[-2:] == ("django", "test")


if __name__ == "__main__":
    sys.exit(main())
