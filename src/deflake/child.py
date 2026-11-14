"""What every test runner does inside a test process deflake started: read what to run and in which
order, record each test with the state it leaked, and seed the random number generator"""

from __future__ import annotations

import contextlib
import functools
import json
import os
import random
import subprocess
import sys
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from typing import Any, TypeVar

from deflake import events, leaks
from deflake.leaks.states import Probed

T = TypeVar("T")


@dataclass(frozen=True)
class Request:
    """What deflake asked this process for.

    output: where to record, None when only the order is asked for, as the reproduce command does.
    ids: the tests to run, None for all; exact: run them in exactly that order, not the seed's
    """

    output: str | None
    seed: int | None
    ids: list[str] | None = None
    exact: bool = False
    listing: bool = False
    probe: dict[str, str] | None = None


def request() -> Request | None:
    """Reads the request and removes its variables, so a test that starts deflake or a test process
    itself does not pass them on. None when deflake did not start this process"""
    values = {key: os.environ.pop(key, None) for key in events.KEYS}
    output, seed = values[events.OUTPUT_KEY], values[events.SEED_KEY]
    if output is None and seed is None:
        return None

    if output is not None:
        watch_processes(output)

    order, only = values[events.ORDER_KEY], values[events.ONLY_KEY]
    ids_file = order or only
    ids = events.read_ids(ids_file) if ids_file else None

    return Request(
        output=output,
        seed=int(seed) if seed else None,
        ids=ids,
        exact=bool(order),
        listing=bool(values[events.LIST_KEY]),
        probe=json.loads(values[events.PROBE_KEY] or "null"),
    )


def watch_processes(output: str) -> None:
    """Record Python-created descendants before returning their PID to project code. Fork callbacks
    are inherited, so grandchildren remain owned after reparenting or creating another session"""
    registry = events.process_file(output)

    def record(pid: int) -> None:
        with contextlib.suppress(OSError):
            identity = events.process_identity(pid)
            if identity is not None:
                events.append(registry, pid=pid, identity=identity)

    original = subprocess.Popen.__init__

    @functools.wraps(original)
    def initialize(process: subprocess.Popen[Any], *args: Any, **kwargs: Any) -> None:
        original(process, *args, **kwargs)
        record(process.pid)

    setattr(subprocess.Popen, original.__name__, initialize)
    os.register_at_fork(after_in_child=lambda: record(os.getpid()))

    def spawn(factory: Callable[..., int], *args: Any, **kwargs: Any) -> int:
        pid = factory(*args, **kwargs)
        record(pid)

        return pid

    for name in ("posix_spawn", "posix_spawnp"):
        factory = getattr(os, name, None)
        if factory is not None:
            setattr(os, name, functools.wraps(factory)(functools.partial(spawn, factory)))


def arrange(
    tests: Sequence[T],
    request: Request,
    id_of: Callable[[T], str],
    path_of: Callable[[T], list[str]],
) -> list[T]:
    """The tests to run, in order: exactly as listed, or shuffled by the seed.

    path_of gives the groups a test is in, outermost first, such as its module and class: the seed
    shuffles the groups and the tests inside each one, but keeps every group together, so fixtures
    and hooks of a group run once, as without deflake
    """
    if request.ids is not None:
        by_id = {id_of(test): test for test in tests}
        chosen = [by_id[id] for id in dict.fromkeys(request.ids) if id in by_id]
        if request.exact:
            return chosen
        tests = chosen

    if request.seed is None:
        return list(tests)

    # The same seed gives the same order however the tests were collected, as in another folder
    return shuffled(sorted(tests, key=id_of), random.Random(request.seed), path_of, 0)


def shuffled(
    tests: list[T],
    generator: random.Random,
    path_of: Callable[[T], list[str]],
    depth: int,
) -> list[T]:
    groups: dict[str | None, list[T]] = {}
    for test in tests:
        path = path_of(test)
        groups.setdefault(path[depth] if depth < len(path) else None, []).append(test)

    if list(groups) == [None]:
        tests = list(tests)
        generator.shuffle(tests)
        return tests

    keys = sorted(groups, key=lambda key: (key is None, key or ""))
    generator.shuffle(keys)
    result: list[T] = []
    for key in keys:
        members = groups[key]
        if key is None:
            members = list(members)
            generator.shuffle(members)
            result.extend(members)
        else:
            result.extend(shuffled(members, generator, path_of, depth + 1))

    return result


def job() -> str | None:
    """The number of this test process among those deflake --jobs runs at once"""
    return os.environ.get(events.JOB_KEY)


def reseed(seed: int | None) -> None:
    """Before every test, so random data a test makes depends on the seed, not on the tests before it.
    Faker, factory_boy and NumPy are seeded too, when the tests use them"""
    if seed is None:
        return

    random.seed(seed)
    faker = sys.modules.get("faker")
    if faker is not None and hasattr(faker, "Faker"):
        quietly(lambda: faker.Faker.seed(seed))

    factory_random = sys.modules.get("factory.random")
    if factory_random is not None:
        quietly(lambda: factory_random.reseed_random(seed))

    numpy_random = sys.modules.get("numpy.random")
    if numpy_random is not None:
        quietly(lambda: numpy_random.seed(seed % 2**32))


def quietly(call: Callable[[], object]) -> None:
    """A library that changed how it is seeded must not break the tests"""
    with contextlib.suppress(Exception):
        call()


class Recorder:
    """Writes the events of one test process (see events)"""

    def __init__(self, output: str, seed: int | None, probe: dict[str, str] | None = None) -> None:
        self.probe = probe
        self.environment: dict[str, Any] | None = None
        self.detector = Probed()
        self.output = output
        self.seed = seed
        events.start(output)

    def started(self, id: str, name: str) -> None:
        if self.probe and id == self.probe["culprit"]:
            self.environment = self.detector.snapshot()

        events.append(self.output, started=id, name=name)

    def finished(
        self,
        id: str,
        name: str,
        *,
        passed: bool,
        skipped: bool = False,
        error: str | None = None,
        leaked: list[str] | None = None,
        flaky: bool = False,
        expected_failure: bool = False,
    ) -> None:
        """flaky: the test is marked to be rerun when it fails"""
        fields: dict[str, Any] = {"id": id, "name": name, "seed": self.seed, "leaks": leaked or []}
        if self.probe and id == self.probe["culprit"] and self.environment is not None:
            after = self.detector.snapshot()
            # Only what the report shows as the test's leaks: a wider fixture set up with it, or the
            # runner, changes things too, and they are not the test's to be a cause of
            changed = [
                name
                for name in self.detector.changed(self.environment, after)
                if any(leak.startswith(self.detector.label(name) + " ") for leak in leaked or [])
            ]
            fields["environment_changes"] = changed
            name = self.probe.get("key", "")
            fields["intervened"] = False
            if name in changed and self.probe["mode"] in {"repair", "reapply"}:
                try:
                    self.detector.restore(name, self.environment.get(name), exists=name in self.environment)
                    if self.probe["mode"] == "reapply":
                        self.detector.restore(name, after.get(name), exists=name in after)
                    fields["intervened"] = True
                except Exception:
                    # State that cannot be put back, or set again, makes the run not applied
                    pass

        if expected_failure:
            fields["expected_failure"] = True

        if flaky:
            fields["flaky"] = True

        if skipped:
            events.append(self.output, skipped=True, **fields)
        else:
            events.append(self.output, passed=passed, error=None if passed else error, **fields)

    def outside(self, error: str | None, leaked: list[str] | None = None) -> None:
        events.append(self.output, outside=error, leaks=leaked or [])

    def listed(self, id: str, name: str, file: str, line: int, end: int, groups: list[list[int]]) -> None:
        """groups: [first line, last line] of every class around the test, innermost first"""
        events.append(self.output, listed=id, name=name, file=file, line=line, end=end, groups=groups)


@contextlib.contextmanager
def recording_errors(recorder: Recorder | None, phase: str) -> Iterator[None]:
    """Preserve errors outside test reports, including exits requested by the runner"""
    try:
        yield
    except BaseException as error:
        record_error(recorder, phase, error)
        raise


def record_error(recorder: Recorder | None, phase: str, error: BaseException) -> None:
    if recorder is not None:
        recorder.outside(f"{phase}: {type(error).__name__}: {error}")


class Watch:
    """The state before a test, to tell what it leaked"""

    def __init__(self) -> None:
        self.before = leaks.snapshot()
        self.taken: dict[int, Any] | None = None
        # Leaks of setup wider than the test that happened inside this window
        self.excused: list[str] = []

    def start_of_session(self) -> None:
        """The session has just set up what it takes away at its end: that is not the test's"""
        fresh = leaks.session_state(self.before)
        self.before = [(detector, fresh.get(id(detector), old)) for detector, old in self.before]

    def end_of_session(self) -> None:
        """The session is about to end: what it takes away is not the test's"""
        if self.taken is None:
            self.taken = leaks.session_state(self.before)

    def leaked(self, now: dict[int, Any] | None = None) -> list[str]:
        """now: the state of the detectors taken once for several windows that end together"""
        taken = {**(now or {}), **(self.taken or {})}

        return leaks.own(leaks.since(self.before, taken), self.excused)
