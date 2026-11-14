"""deflake's part of the Django test runner"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any

from django.conf import settings
from django.test import SimpleTestCase, TransactionTestCase
from django.test.runner import DiscoverRunner
from django.test.utils import iter_test_cases
from django.utils.module_loading import import_string

from deflake import child, unittest_runner
from deflake.django import databases
from deflake.django import leaks as django_leaks

# Options of the runner that change the order of the tests, run them at once or stop them early
OFF = {"parallel": 0, "shuffle": False, "reverse": False, "failfast": False, "pdb": False, "debug_sql": False}


def runner_class() -> type[DiscoverRunner]:
    path = settings.TEST_RUNNER
    base: type[DiscoverRunner] = DiscoverRunner if path.startswith("deflake.") else import_string(path)

    return type("Runner", (Deflake, base), {})


class Deflake(DiscoverRunner):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **{**kwargs, **OFF})
        self.request = child.request() or child.Request(output=None, seed=None)
        output = self.request.output
        self.recorder = child.Recorder(output, self.request.seed, self.request.probe) if output else None
        self.planned: list[unittest.TestCase] = []
        # Every test of the file, before the tests asked for are chosen
        self.found: list[unittest.TestCase] = []
        self.suite_started = False
        if self.recorder is not None:
            django_leaks.install()

    def run_tests(self, test_labels: list[str], **kwargs: Any) -> int:
        test_labels = [module_of(label) for label in test_labels]
        if not self.request.listing:
            try:
                return int(super().run_tests(test_labels, **kwargs))
            except BaseException as error:
                # Errors from run_suite on, the teardown and the project runner's own code after it
                # included, are recorded with the results. An earlier one, as a test database that
                # cannot be created, ends the process: the tests could not run
                if self.suite_started:
                    child.record_error(self.recorder, "Django test run", error)
                raise

        self.setup_test_environment()
        try:
            failed = False
            if self.recorder is not None:
                for test in iter_test_cases(super().build_suite(test_labels)):
                    if unittest_runner.failed_import(test):
                        # Shown as the error it is, not as a test missing at the line or name asked for
                        message = f"error importing {test.id()}: {getattr(test, '_exception', None)!r}"
                        self.recorder.outside(message)
                        print(message, file=sys.stderr)
                        failed = True
                    else:
                        unittest_runner.list_test(self.recorder, test)
        finally:
            self.teardown_test_environment()

        return 1 if failed else 0

    def build_suite(self, test_labels: Any = None, **kwargs: Any) -> unittest.TestSuite:
        tests = list(iter_test_cases(super().build_suite(test_labels, **kwargs)))
        self.found = tests
        if self.request.exact:
            self.planned = child.arrange(tests, self.request, id_of, unittest_runner.groups)
            return self.test_suite(self.planned)

        # Django runs TestCase, then other database tests, then the rest: the seed shuffles each kind
        kinds: dict[int, list[unittest.TestCase]] = {}
        for test in tests:
            kinds.setdefault(self.kind(test), []).append(test)

        chosen = [
            test
            for kind in sorted(kinds)
            for test in child.arrange(kinds[kind], self.request, id_of, unittest_runner.groups)
        ]
        self.planned = chosen

        return self.test_suite(chosen)

    def kind(self, test: unittest.TestCase) -> int:
        if unittest_runner.failed_import(test):
            return -1

        for index, kind in enumerate(self.reorder_by):
            if isinstance(test, kind):
                return index

        return len(self.reorder_by)

    def get_databases(self, suite: unittest.TestSuite) -> Any:
        """The databases of the whole file: run alone or with a culprit, a test that is not a database
        test would otherwise reach the project's own database, which the file's other tests had made
        a test database of"""
        return super().get_databases(self.test_suite(self.found) if self.found else suite)

    def setup_databases(self, **kwargs: Any) -> Any:
        job = child.job()
        if job:
            databases.suffix(job)

        return super().setup_databases(**kwargs)

    def run_suite(self, suite: unittest.TestSuite, **kwargs: Any) -> Any:
        self.suite_started = True
        project = getattr(self, "test_runner")  # noqa: B009
        self.test_runner = recording(project, self)
        try:
            return self.watched_suite(suite, **kwargs)
        finally:
            self.test_runner = project

    def result_class(self, base: Any) -> Any:
        if not (isinstance(base, type) and issubclass(base, unittest.TestResult)):
            base = unittest.TextTestResult

        return unittest_runner.result_class(self.recorder, self.request.seed, self.planned, base)

    def watched_suite(self, suite: unittest.TestSuite, **kwargs: Any) -> Any:
        watch = child.Watch() if self.recorder else None
        original = TransactionTestCase.__dict__["_pre_setup"]
        # The stubs declare a plain method, Django defines a classmethod
        TransactionTestCase._pre_setup = classmethod(eager(original.__func__))  # pyright: ignore[reportAttributeAccessIssue]
        try:
            with unittest_runner.watching(unittest.TestCase, SimpleTestCase):
                result = super().run_suite(suite, **kwargs)
        finally:
            TransactionTestCase._pre_setup = original

        if self.recorder is not None and not isinstance(result, unittest_runner.Recording):
            self.recorder.outside(
                "the project's TEST_RUNNER runs the tests without its test_runner, which deflake "
                "records the tests with"
            )

        unittest_runner.report_outside(self.recorder, watch, result)

        return result


def recording(runner: Any, deflake: Deflake) -> Any:
    """The test runner of the project, recording on top of the result it makes: the runner may rely
    on its own result, as xmlrunner does, which sets it on the runner only when it is made"""

    def make(*args: Any, **kwargs: Any) -> Any:
        made = runner(*args, **kwargs)
        made.resultclass = deflake.result_class(getattr(made, "resultclass", None))

        return made

    return make


def eager(setup: Any) -> Any:
    """A TransactionTestCase loads the fixtures of its first test in setUpClass, before the test is
    called: the test's leaks are counted from the state before that. What the rest of setUpClass
    does, as starting the server of a LiveServerTestCase, is the class's"""

    def _pre_setup(cls: type) -> Any:
        result = unittest_runner.Recording.active
        if result is None or result.recorder is None or result.calling is not None or cls in result.eager:
            return setup(cls)

        before = child.Watch()
        done = setup(cls)
        result.eager[cls] = (before, child.Watch())

        return done

    return _pre_setup


def module_of(label: str) -> str:
    """A test file as the module it is on sys.path, as manage.py set it up: a project may keep its
    apps in a folder of its own, such as apps/"""
    file = Path(label)
    if file.suffix != ".py" or not file.is_file():
        return label

    file = file.resolve()
    roots = [Path(entry or ".").resolve() for entry in sys.path]
    names: list[tuple[str, ...]] = [
        file.relative_to(root).with_suffix("").parts
        for root in roots
        if root.is_dir() and file.is_relative_to(root)
    ]
    valid = [parts for parts in names if all(part.isidentifier() for part in parts)]
    if not valid:
        return label

    shortest: tuple[str, ...] = min(valid, key=len)

    return ".".join(shortest)


def id_of(test: unittest.TestCase) -> str:
    return test.id()
