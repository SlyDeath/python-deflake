"""Runs a test file once in a fresh process and returns one row per test that started, in run order.

The output goes to a file, not a pipe: a process the tests started in the background would keep a
pipe open, and deflake would wait for it. The test process gets a session of its own, without a
terminal, so a timeout or an interrupt stops whatever it started too
"""

from __future__ import annotations

import contextlib
import ctypes
import errno
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import weakref
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeVar

from deflake import events, result_file, tail
from deflake.adapters import Adapter, Error
from deflake.models import OUTSIDE

Row = dict[str, Any]
T = TypeVar("T")

# Seconds a test process may run before it is killed, None for no limit
timeout: int | None = None

OUTSIDE_NAME = "errors outside of tests"
JOB_KEY = events.JOB_KEY

# Test processes running now, stopped all at once on an interrupt
_running: set[subprocess.Popen[bytes]] = set()
_lock = threading.RLock()
_members: weakref.WeakKeyDictionary[subprocess.Popen[bytes], list[tuple[int, str]]] = (
    weakref.WeakKeyDictionary()
)
# The registry follows the process through normal completion and every interrupt/kill path
_descendants: weakref.WeakKeyDictionary[subprocess.Popen[bytes], Path] = weakref.WeakKeyDictionary()
# Set by stop_all: no process starts after it, and one starting at that moment is killed once known
_stopping = threading.Event()
# Processes being started now, which stop_all waits for, so none is left behind when deflake exits
_launching = 0
_launched = threading.Condition(_lock)
# Seconds stop_all waits for processes being started
LAUNCH_WAIT = 10
# Seconds an interrupted test process has to tear down what its tests set up, such as test databases. Less
# than the 10 seconds docker stop and systemd give deflake itself after SIGTERM
STOP_WAIT = 5
# Passes over the group of a leader, as members not yet stopped may fork: each finds the children of the last
GROUP_PASSES = 100
# Test processes already interrupted: a second SIGINT would cut short the teardown of their tests
_interrupted: weakref.WeakSet[subprocess.Popen[bytes]] = weakref.WeakSet()


class NoResults(Error):
    """A test process wrote no results at all. output is what it printed, ending how it ended"""

    def __init__(self, target: str, output: str, ending: str, *, success: bool = False) -> None:
        self.output = output
        self.ending = ending
        self.success = success
        super().__init__(f"{target} produced no test results (the test process {ending}):\n{output}")


@dataclass
class Status:
    code: int | None
    timed_out: int | None = None

    @property
    def ending(self) -> str:
        if self.timed_out is not None:
            return f"timed out after {self.timed_out} seconds"
        if self.code is not None and self.code < 0:
            return f"was killed by {signal.Signals(-self.code).name}"

        return f"exited with status {self.code}"


@dataclass
class Outcome:
    """What one test process left: rows of its tests (None when it wrote none), errors and leaks
    outside of tests, tests it listed, its status and the end of its output"""

    rows: list[Row] | None
    outside: list[Row]
    status: Status
    success: bool
    output: str
    listed: list[Row] = field(default_factory=list)

    @property
    def ran(self) -> bool:
        return bool(self.rows)

    @property
    def failed_test(self) -> bool:
        """A failed test explains why the process failed: a test the process ended during, or one
        that failed in a process that exited on its own, not killed after its tests"""
        rows = self.rows or []
        if any(row.get("group") for row in rows):
            return True

        status = self.status
        # Only the ordinary failed-tests exit code can be explained by a recorded failure
        exited = status.timed_out is None and status.code == 1

        return exited and any(row.get("passed") is False for row in rows)

    @property
    def failed_tests(self) -> bool:
        return any(row.get("passed") is False for row in self.rows or [])

    @property
    def leaks(self) -> list[str]:
        return [leak for row in self.outside for leak in row.get("leaks") or []]


def call(adapter: Adapter, path: str, seed: int, *, env: dict[str, str] | None = None) -> list[Row]:
    """The rows of the tests that ran; raises NoResults when the process wrote none"""
    process = complete(run(adapter, path, seed, env or {}), path)
    rows = process.rows
    assert rows is not None
    status = process.status
    if (
        status.timed_out is not None
        or (status.code is not None and status.code < 0)
        or (not process.success and status.code != 1)
        or not outside_row(process, seed)["passed"]
    ):
        # A completed test does not make an interrupted control process valid evidence
        return [{**row, "process_interrupted": True} for row in rows]

    return rows


def whole_file(adapter: Adapter, path: str, seed: int, env: dict[str, str] | None = None) -> list[Row]:
    """Runs the whole file and adds an OUTSIDE row telling whether the process failed on its own.
    A process that wrote no results at all is one failed OUTSIDE row carrying its output"""
    process = run(adapter, path, seed, env or {})
    if process.rows is None:
        return [no_results_row(seed, process)]

    return [*process.rows, outside_row(process, seed)]


def some(
    adapter: Adapter,
    path: str,
    seed: int,
    only: list[str],
    env: dict[str, str] | None = None,
) -> list[Row]:
    """Runs only some tests. A process that wrote no results, or where none of them started, is a
    failed OUTSIDE row"""
    with tempfile.TemporaryDirectory(prefix="deflake-") as folder:
        ids = Path(folder) / "only.json"
        events.write_ids(ids, only)
        process = run(adapter, path, seed, {**(env or {}), events.ONLY_KEY: str(ids)})

    return process.rows if process.rows else [no_results_row(seed, process)]


def in_order(
    adapter: Adapter,
    path: str,
    ids: list[str],
    seed: int,
    probe: dict[str, str] | None = None,
) -> list[Row]:
    """Runs exactly these tests in exactly this order"""
    with tempfile.TemporaryDirectory(prefix="deflake-") as folder:
        order = Path(folder) / "order.json"
        events.write_ids(order, ids)
        env = {events.ORDER_KEY: str(order)}
        if probe is not None:
            env[events.PROBE_KEY] = json.dumps(probe)

        return call(adapter, path, seed, env=env)


def order(adapter: Adapter, path: str, seed: int) -> list[str]:
    """The ids of the file's tests in the order they ran with this seed"""
    return [row["id"] for row in call(adapter, path, seed)]


def listed(adapter: Adapter, path: str) -> list[Row]:
    """Every test of the file, with where it is defined, without running any"""
    process = run(adapter, path, 0, {events.LIST_KEY: "1"})
    if not process.listed and not (process.rows is not None and process.success):
        raise NoResults(path, process.output, process.status.ending, success=process.success)

    return process.listed


def retrying(attempt: Callable[[], T], attempts: int = 3) -> T:
    """Tries again when the test process wrote no results, as a flaky start does now and then"""
    for _ in range(attempts - 1):
        try:
            return attempt()
        except NoResults:
            pass

    return attempt()


def no_results_row(seed: int, process: Outcome) -> Row:
    message = f"the test process {process.status.ending} before any test finished"
    failures = outside_failures(process)
    error = f"{message}:\n{failures}" if failures else tail.with_tail(message, process.output)

    return {
        "id": OUTSIDE,
        "name": OUTSIDE_NAME,
        "passed": False,
        "seed": seed,
        "error": error,
        "output": process.output,
        "ending": process.status.ending,
        "leaks": [],
    }


def complete(process: Outcome, target: str) -> Outcome:
    if process.rows is None:
        raise NoResults(target, process.output, process.status.ending, success=process.success)

    return process


def run(adapter: Adapter, path: str, seed: int, env: dict[str, str]) -> Outcome:
    with tempfile.TemporaryDirectory(prefix="deflake-") as folder:
        workdir = Path(folder)
        output = workdir / "results.jsonl"
        log = workdir / "output.log"
        # Neither the request to a test process that started this deflake nor its job number goes on
        withheld = {*events.KEYS, JOB_KEY}
        child_env = {key: value for key, value in os.environ.items() if key not in withheld}
        child_env.update(env)
        child_env.update(
            {
                events.OUTPUT_KEY: str(output),
                events.SEED_KEY: str(seed),
                # A crash prints the traceback of every thread, and nothing printed is lost on a kill
                "PYTHONFAULTHANDLER": "1",
                "PYTHONUNBUFFERED": "1",
            }
        )
        status = start(adapter.command(path, workdir), child_env, log)
        rows = result_file.read(output) if output.exists() else None
        success = status.timed_out is None and status.code is not None and adapter.success(status.code)
        return finish(rows, seed, status, success, tail.read(log))


def start(command: list[str], env: dict[str, str], log: Path) -> Status:
    process = launched(command, env, log)
    try:
        try:
            return Status(process.wait(timeout))
        except subprocess.TimeoutExpired:
            kill(process)
            process.wait()
            return Status(process.returncode, timed_out=timeout)
    except KeyboardInterrupt:
        interrupt([process])
        raise
    finally:
        # Also what the tests started and left running, which would outlive deflake
        kill(process)
        if process.returncode is None:
            process.wait()
        with _lock:
            _running.discard(process)


def stop_descendants(registry: Path) -> None:
    """Kill registered descendants only while their OS birth identity still matches the run"""
    seen: set[tuple[int, str]] = set()
    while True:
        try:
            rows = result_file.read(registry)
        except FileNotFoundError:
            # Another cleanup can finish the run and remove its temporary directory
            return

        fresh = []
        for row in rows:
            pid, identity = row.get("pid"), row.get("identity")
            if not isinstance(pid, int) or pid <= 0 or not isinstance(identity, str):
                continue
            if (pid, identity) not in seen:
                seen.add((pid, identity))
                fresh.append((pid, identity))

        if not fresh:
            return

        # One that cannot be signalled, as a member of a group of another user, keeps no other alive
        errors = []
        for pid, identity in fresh:
            if events.process_identity(pid) != identity:
                continue

            # A server started in a session of its own leads a group: its members go with it
            members: list[tuple[int, str]] = []
            try:
                if leads_group(pid):
                    freeze_group(pid, identity, members)
            except Error as error:
                errors.append(error)
            finally:
                # Even when a second Ctrl-C cuts the passes short: none is left stopped. The leader last,
                # as its death would continue members of a group orphaned by it
                try:
                    kill_owned([*members, (pid, identity)])
                except Error as error:
                    errors.append(error)

        if errors:
            raise errors[0]


def freeze_group(pid: int, identity: str, members: list[tuple[int, str]]) -> None:
    """Stop the members of the group the process leads, into members, so that none forks out of reach.
    Stopped, they keep the group's number from being reused, and every pass over it finds no one else's.
    One that cannot be stopped leaves the others stopped all the same, the first error raised after"""
    errors = []
    try:
        signal_owned(pid, identity, signal.SIGSTOP)
    except Error as error:
        errors.append(error)

    if events.process_identity(pid) == identity:
        for _ in range(GROUP_PASSES):
            known = {member for member, _ in members}
            fresh = [
                (member, birth)
                for member, birth in group_targets(pid)
                if member != pid and member not in known
            ]
            if not fresh:
                break

            for member, birth in fresh:
                members.append((member, birth))
                try:
                    signal_owned(member, birth, signal.SIGSTOP)
                except Error as error:
                    errors.append(error)

    if errors:
        raise errors[0]


def kill_owned(targets: list[tuple[int, str]]) -> None:
    """SIGKILL every target, the first error raised once all are signalled"""
    errors = []
    for pid, identity in targets:
        try:
            signal_owned(pid, identity, signal.SIGKILL)
        except Error as error:
            errors.append(error)

    if errors:
        raise errors[0]


def leads_group(pid: int) -> bool:
    try:
        return os.getpgid(pid) == pid
    except OSError:
        return False


def require_safe_signals() -> None:
    """Refuse to launch tests when kernel-bound signalling is unavailable"""
    try:
        if sys.platform == "darwin":
            library = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
            if (
                hasattr(library, "proc_signal_with_audittoken")
                and events.audit_token(os.getpid()) is not None
            ):
                return
        elif sys.platform.startswith("linux") and hasattr(signal, "pidfd_send_signal"):
            descriptor = os.pidfd_open(os.getpid())
            os.close(descriptor)
            return
    except (OSError, AttributeError):
        pass

    raise Error("safe process signalling requires Linux pidfd support or the macOS audit-token API")


def signal_owned(pid: int, identity: str, number: int) -> None:
    """Signal a kernel-bound identity; never fall back to a numeric PID or process group"""
    try:
        if sys.platform == "darwin":
            deadline = time.monotonic() + STOP_WAIT
            library = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
            while True:
                raw = events.audit_token(pid)
                # Recheck birth after every token acquisition, including every retry after exec
                if events.process_identity(pid) != identity:
                    return
                if raw is not None:
                    token = (ctypes.c_uint * 8).from_buffer_copy(raw)
                    if token[5] != pid:
                        return
                    result = library.proc_signal_with_audittoken(ctypes.byref(token), number)
                    if result == 0:
                        return
                    if result not in {errno.ESRCH, errno.EINTR}:
                        raise Error(f"could not signal owned process {pid}: {os.strerror(result)}")
                if time.monotonic() >= deadline:
                    raise Error(f"could not signal owned process {pid}: its audit token kept changing")
                time.sleep(0.001)
        elif sys.platform.startswith("linux") and hasattr(os, "pidfd_open"):
            descriptor = os.pidfd_open(pid)
            try:
                if events.process_identity(pid) == identity:
                    signal.pidfd_send_signal(descriptor, number)
            finally:
                os.close(descriptor)
    except (OSError, ValueError, AttributeError):
        # Missing kernel support or a vanished target never authorizes an unsafe fallback
        return


def group_targets(pid: int) -> list[tuple[int, str]]:
    """Snapshot group members while the caller keeps its child leader unreaped"""
    if sys.platform == "darwin":
        library = ctypes.CDLL("/usr/lib/libproc.dylib")
        size = library.proc_listpgrppids(pid, None, 0)
        data = (ctypes.c_int * (max(0, size) + 32))()
        count = library.proc_listpgrppids(pid, data, ctypes.sizeof(data))
        candidates = list(data)[: max(0, count)]
    else:
        # Linux: launch refuses other systems
        candidates = [int(path.name) for path in Path("/proc").iterdir() if path.name.isdigit()]

    targets = []
    for member in candidates:
        identity = events.process_identity(member)
        with contextlib.suppress(ProcessLookupError, PermissionError):
            if identity is not None and os.getpgid(member) == pid:
                targets.append((member, identity))

    return targets


class TestProcess(subprocess.Popen[bytes]):
    """Preserve group identities before waitpid releases the leader's PID for reuse"""

    def wait(self, timeout: float | None = None) -> int:
        # Keep reap locks available to the thread delivering an interrupt
        if timeout is not None:
            return super().wait(timeout)

        while True:
            try:
                return super().wait(0.1)
            except subprocess.TimeoutExpired:
                pass

    def _try_wait(self, wait_flags: int) -> tuple[int, int]:
        # Popen holds _waitpid_lock here on supported CPython versions
        flags = os.WEXITED | os.WNOWAIT | (os.WNOHANG if wait_flags else 0)
        library = ctypes.CDLL(None, use_errno=True)
        info = (ctypes.c_long * 32)()
        while library.waitid(os.P_PID, self.pid, ctypes.byref(info), flags) != 0:
            error = ctypes.get_errno()
            if error == 4:
                continue
            if error == 10:
                return self.pid, 0
            raise OSError(error, os.strerror(error))

        if ctypes.c_int.from_buffer(info).value == 0:
            return 0, 0

        _members[self] = group_targets(self.pid)
        pid, status = super()._try_wait(wait_flags)  # type: ignore[misc]  # CPython POSIX Popen hook

        return int(pid), int(status)


def targets_of(process: subprocess.Popen[bytes]) -> list[tuple[int, str]]:
    # Serialize with Popen's wait/reap: a live or zombie child pins its PID until captured
    with process._waitpid_lock:  # type: ignore[attr-defined]  # CPython POSIX Popen reap lock
        if process.returncode is None:
            _members[process] = group_targets(process.pid)

        return _members.get(process, [])


def launched(command: list[str], env: dict[str, str], log: Path) -> subprocess.Popen[bytes]:
    """The started process, known to stop_all. The main thread, where an interrupt lands, starts it
    in another thread and waits: a process that thread starts after stop_all is killed at once"""
    if threading.current_thread() is not threading.main_thread():
        return launch(command, env, log)

    done = threading.Event()
    outcome: list[Any] = []

    def run() -> None:
        try:
            outcome.append(launch(command, env, log))
        except BaseException as error:
            outcome.append(error)
        finally:
            done.set()

    threading.Thread(target=run, daemon=True).start()
    while not done.wait(0.05):
        pass

    if isinstance(outcome[0], BaseException):
        raise outcome[0]

    process: subprocess.Popen[bytes] = outcome[0]

    return process


def launch(command: list[str], env: dict[str, str], log: Path) -> subprocess.Popen[bytes]:
    global _launching
    require_safe_signals()
    with _lock:
        if _stopping.is_set():
            raise KeyboardInterrupt
        _launching += 1

    process = None
    try:
        with log.open("wb") as out:
            process = TestProcess(
                command,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )

        return process
    finally:
        with _lock:
            _launching -= 1
            if process is not None:
                output = env.get(events.OUTPUT_KEY)
                if output is not None:
                    _descendants[process] = events.process_file(output)
                _running.add(process)
                if _stopping.is_set():
                    kill(process)
            _launched.notify_all()


def stop_all() -> None:
    """Stops every test process running now and kills those started from now on, as after an interrupt"""
    with _lock:
        _stopping.set()
        try:
            _launched.wait_for(lambda: _launching == 0, LAUNCH_WAIT)
        except KeyboardInterrupt:
            # A second interrupt while processes are being started kills the running ones at once
            for process in _running:
                kill(process)
            raise
        processes = list(_running)

    interrupt(processes)


def resume() -> None:
    """Test processes may run again, as stop_all stopped the ones of a former call"""
    _stopping.clear()


def interrupt(processes: list[subprocess.Popen[bytes]]) -> None:
    """Interrupts the test processes as Ctrl-C does, so their tests tear down what they set up, then kills
    what is left. A second interrupt while they tear down kills them at once"""
    with _lock:
        fresh = [process for process in processes if process not in _interrupted]
        _interrupted.update(fresh)

    for process in fresh:
        for pid, identity in targets_of(process):
            signal_owned(pid, identity, signal.SIGINT)

    deadline = time.monotonic() + STOP_WAIT
    try:
        for process in processes:
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(max(0.0, deadline - time.monotonic()))
    finally:
        for process in processes:
            kill(process)


def kill(process: subprocess.Popen[bytes]) -> None:
    for pid, identity in targets_of(process):
        signal_owned(pid, identity, signal.SIGKILL)

    with _lock:
        registry = _descendants.get(process)

    if registry is not None:
        stop_descendants(registry)


def finish(rows: list[Row] | None, seed: int, status: Status, success: bool, output: str) -> Outcome:
    """A test that started and never finished took its process down with it, so it counts as
    failed. A process that wrote nothing, or failed before any test, has no rows"""
    if rows is None:
        return Outcome(None, [], status, success, output)

    outside = [row for row in rows if "outside" in row]
    listed = [row for row in rows if "listed" in row]
    tests = [row for row in rows if "outside" not in row and "listed" not in row]

    outcome = Outcome(finished_tests(tests, seed, status, output), outside, status, success, output, listed)
    if not outcome.rows and not success and not outside:
        outcome.rows = None

    return outcome


def finished_tests(rows: list[Row], seed: int, status: Status, output: str) -> list[Row]:
    done = [row for row in rows if "id" in row]
    finished = {row["id"] for row in done}
    message = f"the test process {status.ending} during this test"
    lost: list[Row] = []

    started = [row for row in rows if "started" in row]
    for row in started:
        if row["started"] in finished or any(other["id"] == row["started"] for other in lost):
            continue
        # What the output shows before the crash changes with the order of the tests
        lost.append(
            {
                "id": row["started"],
                "name": row["name"],
                "passed": False,
                "seed": seed,
                "error": tail.with_tail(message, output),
                "group": message,
                "timed_out": status.timed_out is not None,
                "leaks": [],
            }
        )

    return done + lost


def outside_row(process: Outcome, seed: int) -> Row:
    """A process that failed while every test passed broke somewhere else, for example in a hook.
    Errors outside of tests the runner told about count even when a test failed"""
    error = outside_failures(process)
    passed = error is None and (process.success or process.failed_test)
    row: Row = {
        "id": OUTSIDE,
        "name": OUTSIDE_NAME,
        "passed": passed,
        "seed": seed,
        "error": None if passed else error,
        "leaks": process.leaks,
        "ran": process.ran,
    }

    if not passed and error is None:
        why = "after its tests" if process.failed_tests else "although no test failed"
        message = f"the test process {process.status.ending} {why}"
        row["error"] = tail.with_tail(message, process.output)
        # What the output shows changes with the order of the tests
        row["group"] = message

    return row


def outside_failures(process: Outcome) -> str | None:
    """Sorted, as the order they happened in changes with the seed"""
    errors = sorted({row["outside"] for row in process.outside if row.get("outside")})

    return "\n".join(errors) if errors else None
