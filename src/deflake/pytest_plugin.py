"""The pytest side of deflake, loaded with -p deflake.pytest_plugin. Does nothing unless deflake
started the process or DEFLAKE_SEED is set, as in the reproduce command"""

from __future__ import annotations

import inspect
import signal
import sys
import types
import unittest
from collections.abc import Generator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from deflake import child, leaks
from deflake.leaks import warning_filters
from deflake.leaks.modules import project_file
from deflake.leaks.process import handler_changed

# Plugins that change the order of the tests or run them again, which would hide failures or make
# a seed run another order. Their options still parse, so a project's addopts keeps working
DISABLED = ("randomly", "random_order", "xdist", "xdist.looponfail", "rerunfailures", "flaky")

# Options that select, reorder or stop the tests on their own, and stop at a debugger
OFF = {
    "lf": False,
    "failedfirst": False,
    "newfirst": False,
    "stepwise": False,
    "stepwise_skip": False,
    "exitfirst": False,
    "maxfail": 0,
    "usepdb": False,
    "trace": False,
}

NAME = "deflake-recorder"


@pytest.hookimpl(tryfirst=True)
def pytest_cmdline_main(config: pytest.Config) -> None:
    request = child.request()
    if request is None:
        return

    for name in DISABLED:
        if config.pluginmanager.get_plugin(name) is not None:
            config.pluginmanager.unregister(name=name)

    # Options that select, reorder or stop the tests on their own
    for option, value in OFF.items():
        if hasattr(config.option, option):
            setattr(config.option, option, value)

    if config.pluginmanager.has_plugin(NAME):
        return

    config.pluginmanager.register(Recorder(request), NAME)


class Recorder:
    def pytest_configure(self, config: pytest.Config) -> None:
        # Markers of the disabled plugins, so --strict-markers still accepts them
        config.addinivalue_line("markers", "flaky: rerun a failing test (disabled by deflake)")
        config.addinivalue_line("markers", "xdist_group(name): run tests in one worker (disabled by deflake)")
        config.addinivalue_line("markers", "random_order: shuffle the tests (disabled by deflake)")

    def __init__(self, request: child.Request) -> None:
        self.request = request
        self.recorder = (
            child.Recorder(request.output, request.seed, request.probe) if request.output else None
        )
        # Reports of the test that runs now, by phase
        self.reports: list[pytest.TestReport] = []
        # Changes of fixtures wider than a test, set up or torn down while the test ran: not its leaks
        self.excused: list[str] = []
        # Every leak the tests reported so far
        self.reported: list[str] = []
        # What fixtures wider than a test changed in their setup and teardown so far
        self.settled: list[str] = []
        self.scopes: dict[int, Scope] = {}
        # What pytest-timeout's timer of the test that runs now changes: not the test's
        self.timer: list[str] = []
        # Tests so far that ran with pytest-timeout's signal timer
        self.timed = 0
        # Failed teardown reports of the test that runs now whose error is of a wider fixture, by id
        self.wide_reports: set[int] = set()
        # The test's own error of a teardown report that also has errors of wider fixtures, by its id
        self.errors: dict[int, str] = {}
        # Fixtures wider than a test torn down after the test that runs now, by the id of their definition
        self.closed: dict[int, Scope] = {}
        # The state before the test that runs now
        self.watch: child.Watch | None = None
        # Whether the test that runs now is being torn down
        self.tearing_down = False
        # The state before unittest tests a decorator skipped, and their excused changes, for the
        # next test of their class
        self.inherited: tuple[type, child.Watch, list[str]] | None = None

    @pytest.hookimpl(wrapper=True, tryfirst=True)
    def pytest_sessionfinish(self, session: pytest.Session) -> Generator[None, Any, Any]:
        """A failed session is not explained solely by its completed test reports"""
        with child.recording_errors(self.recorder, "pytest session finalization"):
            try:
                return (yield)
            finally:
                reason = session.shouldfail or session.shouldstop
                if self.recorder is not None and reason:
                    self.recorder.outside(f"pytest session did not finish normally: {reason}")

    # Before other plugins: pytest-django then puts the tests in Django's order, by kind, keeping the
    # order of each kind
    @pytest.hookimpl(wrapper=True, tryfirst=True)
    def pytest_collection_modifyitems(
        self,
        session: pytest.Session,
        config: pytest.Config,
        items: list[pytest.Item],
    ) -> Generator[None, Any, Any]:
        self.arrange(config, items)

        return (yield)

    def arrange(self, config: pytest.Config, items: list[pytest.Item]) -> None:
        if self.request.listing:
            self.list_tests(items)
            chosen: list[pytest.Item] = []
        else:
            chosen = child.arrange(items, self.request, lambda item: item.nodeid, groups)

        kept = {id(item) for item in chosen}
        deselected = [item for item in items if id(item) not in kept]
        if deselected:
            config.hook.pytest_deselected(items=deselected)

        items[:] = chosen

    def list_tests(self, items: list[pytest.Item]) -> None:
        if self.recorder is None:
            return

        for item in items:
            obj = getattr(item, "obj", None)
            line, end = lines_of(obj, (item.location[1] or 0) + 1)
            # A test of a mixin from another file has no line in this one
            if not in_file(obj, item.path):
                line = end = 0
            classes = [list(lines_of(cls, 0)) for cls in classes_of(item) if in_file(cls, item.path)]
            self.recorder.listed(item.nodeid, name_of(item), str(item.path), line, end, classes)

    @pytest.hookimpl(wrapper=True)
    def pytest_runtest_protocol(
        self,
        item: pytest.Item,
        nextitem: pytest.Item | None,
    ) -> Generator[None, Any, Any]:
        child.reseed(self.request.seed)
        if self.recorder is None:
            return (yield)

        self.reports = []
        self.closed = {}
        self.timer = []
        self.wide_reports = set()
        self.errors = {}
        cls = getattr(item, "cls", None)
        # Django's detectors go in before the first snapshot, so the first test is watched by them too
        django_leaks()
        inherited, self.inherited = self.inherited, None
        if inherited is not None and inherited[0] is cls:
            watch, self.excused = inherited[1], inherited[2]
        else:
            watch, self.excused = child.Watch(), []

        self.recorder.started(item.nodeid, name_of(item))
        self.tearing_down = False
        self.watch = watch
        result = yield
        self.watch = None
        # pytest's warnings plugin puts the filters back after every test, outside of this window
        restored = [warning_filters.CHANGED] if item.config.pluginmanager.has_plugin("warnings") else []
        leaked = leaks.own(watch.leaked(), self.excused + self.timer + restored)
        self.reported.extend(leaked)
        if cls is not None and skipped_by_decorator(item, cls):
            # The setup of a unittest class for its first test, as a Django TransactionTestCase
            # loading its fixtures early, is used by the first test it does not skip: that one is
            # watched from before the setup
            self.inherited = (cls, watch, self.excused + leaked)

        self.finish(item, leaked)

        return result

    @pytest.hookimpl(wrapper=True)
    def pytest_fixture_setup(
        self,
        fixturedef: pytest.FixtureDef[Any],
        request: pytest.FixtureRequest,
    ) -> Generator[None, Any, Any]:
        """What a fixture of a class, module or session sets up is not the leak of the test that
        happens to use it first, nor is what it puts back the leak of the last one"""
        # A class fixture of a test outside a class is set up and torn down with that test alone, inside its
        # window, as pytest-django's autouse one for every function. What it leaves is the test's, as with a
        # function fixture, also for a fixture of an installed plugin
        if self.recorder is None or fixturedef.scope == "function" or isinstance(request.node, pytest.Item):
            return (yield)

        # pytest-timeout sets the timer of a test before its fixtures: the test setting this one up counts
        timed = self.timed - len(self.timer)
        scope = Scope(fixturedef.argname, fixturedef.scope, child.Watch(), len(self.reported), timed)
        try:
            value = yield
            django = DJANGO.get(fixturedef.argname)
            if fixturedef.argname == "django_db_setup" and self.watch is not None:
                # The test databases did not exist when the test that set them up started
                self.watch.start_of_session()
            return django(value) if django else value
        finally:
            setup = scope.watch.leaked()
            self.excused.extend(setup)
            self.settled.extend(setup)
            scope.settled = len(self.settled)
            self.scopes[id(fixturedef)] = scope
            # Finalizers run last in, first out: this one runs before the fixture's own teardown
            fixturedef.addfinalizer(lambda: self.torn_down(scope))

    # pytest-timeout's own returns True and so ends the hook: this one goes first
    @pytest.hookimpl(optionalhook=True, tryfirst=True)
    # pytest-timeout's Settings is untyped
    def pytest_timeout_set_timer(self, item: pytest.Item, settings: Any) -> None:
        """pytest-timeout's signal method sets SIGALRM to its default when it stops the timer, whatever
        handler there was before the test: that is not the test's leak"""
        if settings.method == "signal" and hasattr(signal, "SIGALRM"):
            self.timer = [handler_changed(signal.SIGALRM)]
            self.timed += 1

    @pytest.hookimpl(tryfirst=True)
    def pytest_runtest_teardown(self, item: pytest.Item, nextitem: pytest.Item | None) -> None:
        self.tearing_down = True

    def torn_down(self, scope: Scope) -> None:
        # The last test tears down the session, pytest-django's test databases with it
        if self.tearing_down and self.watch is not None and scope.scope == "session":
            self.watch.end_of_session()

        scope.teardown = child.Watch()

    def pytest_fixture_post_finalizer(self, fixturedef: pytest.FixtureDef[Any]) -> None:
        scope = self.scopes.get(id(fixturedef))
        if self.recorder is None or scope is None or scope.teardown is None:
            return

        if self.tearing_down:
            self.closed[id(fixturedef)] = scope

        del self.scopes[id(fixturedef)]
        # Every window here ends now: the state is taken once
        now = leaks.current()
        teardown = scope.teardown.leaked(now)
        self.excused.extend(teardown)
        # The test running now has not reported its leaks yet; what fixtures set up during it is not its
        running = leaks.own(self.watch.leaked(now), self.excused) if self.watch else []
        # What narrower fixtures set up and tore down while this one was set up is theirs, not its
        # Nor what a timer changed, if a test ran with one while it was set up
        timers = [handler_changed(signal.SIGALRM)] if self.timed > scope.timed else []
        others = [*self.reported[scope.reported :], *self.settled[scope.settled :], *running, *timers]
        self.settled.extend(teardown)
        own = leaks.own(scope.watch.leaked(now), others)
        # A fixture of an installed plugin, as pytest-django's, sets up what the tests need
        if own and not project_fixture(fixturedef):
            self.reported.extend(own)
        elif own:
            # So wider fixtures torn down later do not report them again
            self.reported.extend(own)
            self.recorder.outside(None, [f"{label(scope)}, outside its tests: {leak}" for leak in own])

    @pytest.hookimpl(wrapper=True)
    def pytest_runtest_makereport(
        self,
        item: pytest.Item,
        call: pytest.CallInfo[None],
    ) -> Generator[None, pytest.TestReport, pytest.TestReport]:
        """pytest tears down a fixture of a class, module or session after the last test that uses it,
        and puts its error in that test's report: the error is not the test's, as under unittest"""
        report = yield
        if self.recorder is None or call.when != "teardown" or not report.failed or call.excinfo is None:
            return report

        errors = self.wide_errors(call.excinfo.value)
        wide = [(error, scope) for error, scope in errors if scope is not None]
        if len(errors) == 1 and wide:
            self.recorder.outside(f"error in teardown of {label(wide[0][1])}:\n{longrepr(report)}")
        else:
            for error, scope in wide:
                text = f"{type(error).__name__}: {error}"
                self.recorder.outside(f"error in teardown of {label(scope)}: {text}")

        # With an error of the test's own teardown as well, the report stays the test's, with the error
        # it has when no wider fixture is torn down after it
        own = [error for error, scope in errors if scope is None]
        if wide and not own:
            self.wide_reports.add(id(report))
        elif wide and len(own) == 1:
            self.errors[id(report)] = error_of(own[0])

        return report

    def wide_errors(
        self,
        error: BaseException,
        scope: Scope | None = None,
    ) -> list[tuple[BaseException, Scope | None]]:
        """Every error of a teardown, several in a group, with the fixture wider than the test it is of"""
        scope = self.wide_fixture(error) or scope
        if isinstance(error, BaseExceptionGroup):
            return [pair for inner in error.exceptions for pair in self.wide_errors(inner, scope)]

        return [(error, scope)]

    def wide_fixture(self, error: BaseException) -> Scope | None:
        """The fixture wider than the test, torn down after it, whose FixtureDef.finish the error left"""
        # The innermost: the finish of a fixture runs the finish of the fixtures that depend on it
        found = None
        entry = error.__traceback__
        while entry is not None:
            frame = entry.tb_frame
            if frame.f_code.co_name == "finish":
                found = self.closed.get(id(frame.f_locals.get("self"))) or found
            entry = entry.tb_next

        return found

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        self.reports.append(report)

    def pytest_collectreport(self, report: pytest.CollectReport) -> None:
        if self.recorder is not None and report.failed:
            self.recorder.outside(f"error collecting {report.nodeid or 'tests'}:\n{longrepr(report)}")

    def finish(self, item: pytest.Item, leaked: list[str]) -> None:
        assert self.recorder is not None
        failed = next(
            (report for report in self.reports if report.failed and id(report) not in self.wide_reports),
            None,
        )
        name = name_of(item)
        flaky = item.get_closest_marker("flaky") is not None
        if failed is not None:
            error = self.errors.get(id(failed)) or message(failed)
            if failed.when != "call":
                error = f"error in {failed.when}: {error}"
            self.recorder.finished(item.nodeid, name, passed=False, error=error, leaked=leaked, flaky=flaky)
        elif any(report.skipped and hasattr(report, "wasxfail") for report in self.reports):
            self.recorder.finished(
                item.nodeid,
                name,
                passed=True,
                expected_failure=True,
                leaked=leaked,
                flaky=flaky,
            )
        elif any(report.skipped for report in self.reports):
            self.recorder.finished(item.nodeid, name, passed=True, skipped=True, leaked=leaked, flaky=flaky)
        else:
            self.recorder.finished(item.nodeid, name, passed=True, leaked=leaked, flaky=flaky)


@dataclass
class Scope:
    """A fixture wider than a test: the state before its setup, how many leaks the tests had
    reported and how many ran with a signal timer by then, how many changes of fixtures were seen by
    the end of its setup, and the state before its teardown"""

    name: str
    scope: str
    watch: child.Watch
    reported: int
    timed: int
    teardown: child.Watch | None = None
    settled: int = 0


def django_leaks() -> None:
    """Django's detectors too, once pytest-django or the project has configured Django"""
    if "django.conf" not in sys.modules:
        return

    from django.conf import settings

    if settings.configured:
        from deflake.django.leaks import install

        install()


def django_db_blocker(blocker: Any) -> Any:
    """pytest-django blocks the database outside of the tests that ask for it, deflake still counts
    the rows of every test"""
    from deflake.django import databases

    databases.blocker = blocker

    return blocker


def django_db_modify_db_settings(value: Any) -> Any:
    """Every process of deflake --jobs gets test databases of its own"""
    job = child.job()
    if job:
        from deflake.django import databases

        databases.suffix(job)

    return value


# pytest-django's fixtures deflake takes part in, by name
DJANGO = {
    "django_db_blocker": django_db_blocker,
    "django_db_modify_db_settings": django_db_modify_db_settings,
}


def project_fixture(fixturedef: pytest.FixtureDef[Any]) -> bool:
    """A fixture of the project, also one pytest makes of the project's setup_module or setUpClass,
    whose function is pytest's own and holds the project's"""
    closure = getattr(fixturedef.func, "__closure__", None) or ()
    for obj in [fixturedef.func, *(cell.cell_contents for cell in closure if cell_filled(cell))]:
        try:
            file = inspect.getsourcefile(obj)
        except (TypeError, OSError):
            continue
        if file is not None and project_file(file):
            return True

    return False


def cell_filled(cell: types.CellType) -> bool:
    """A closure's variable not assigned yet has no contents"""
    try:
        _ = cell.cell_contents
    except ValueError:
        return False

    return True


def name_of(item: pytest.Item) -> str:
    """The test's node id without its file: Class::test_name[param]"""
    return item.nodeid.split("::", 1)[1] if "::" in item.nodeid else item.nodeid


def groups(item: pytest.Item) -> list[str]:
    """The node ids of the module and classes the test is in, outermost first"""
    return [node.nodeid for node in item.listchain()[1:-1] if isinstance(node, (pytest.Module, pytest.Class))]


def classes_of(item: pytest.Item) -> list[type]:
    return [node.obj for node in reversed(item.listchain()) if isinstance(node, pytest.Class)]


def source_file(obj: Any) -> str | None:
    try:
        return inspect.getsourcefile(inspect.unwrap(obj))
    except TypeError:
        return None


def skipped_by_decorator(item: pytest.Item, cls: type) -> bool:
    if not issubclass(cls, unittest.TestCase):
        return False

    return any(getattr(owner, "__unittest_skip__", False) for owner in (cls, getattr(item, "obj", None)))


def in_file(obj: Any, path: Path) -> bool:
    """Whether a function or class is defined in the file, or its source is unknown"""
    source = source_file(obj)

    return source is None or Path(source).resolve() == path.resolve()


def lines_of(obj: Any, line: int) -> tuple[int, int]:
    """The first and last line of a function or class, or just the line when the source is unknown"""
    try:
        source, first = inspect.getsourcelines(inspect.unwrap(obj))
    except (TypeError, OSError):
        return line, line

    return first, first + len(source) - 1


def message(report: pytest.TestReport) -> str:
    crash = getattr(report.longrepr, "reprcrash", None)
    if crash is not None:
        text = str(crash.message)
        location = Path(str(crash.path)).name
        return f"{text}\n  at {location}:{crash.lineno}"

    return longrepr(report)


def error_of(error: BaseException) -> str:
    """The error as pytest shows the crash of a report, frames hidden by __tracebackhide__ left out"""
    info = pytest.ExceptionInfo.from_exception(error)
    # What reprcrash of a report is made of, the same from pytest 8.0 on; private, so it may go
    reprcrash = getattr(info, "_getreprcrash", None)
    crash = reprcrash() if reprcrash is not None else None
    if crash is None:
        return info.exconly(tryshort=True)

    return f"{crash.message}\n  at {Path(crash.path).name}:{crash.lineno}"


def label(scope: Scope) -> str:
    return f"fixture {scope.name} ({scope.scope} scope)"


def longrepr(report: pytest.TestReport | pytest.CollectReport) -> str:
    text = report.longreprtext.strip()

    return "\n".join(text.splitlines()[-20:])
