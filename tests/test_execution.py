import _thread
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest

from deflake import execution
from deflake.adapters import Adapter

# These calls go to a patched run, which never uses the adapter
NO_ADAPTER = cast(Adapter, None)


def test_an_interrupt_while_a_process_starts_leaves_it_stopped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started: list[subprocess.Popen[bytes]] = []
    # Popen picks the text overload for untyped arguments: these processes write bytes
    real = cast(Callable[..., subprocess.Popen[bytes]], execution.TestProcess)

    def slow_popen(*args: Any, **kwargs: Any) -> subprocess.Popen[bytes]:
        process = real(*args, **kwargs)
        started.append(process)
        _thread.interrupt_main()
        # Still starting when the interrupt reaches the main thread
        time.sleep(0.5)

        return process

    monkeypatch.setattr(execution, "TestProcess", slow_popen)
    command = [sys.executable, "-c", "import time; time.sleep(30)"]
    try:
        with pytest.raises(KeyboardInterrupt):
            execution.start(command, {}, tmp_path / "log")
        execution.stop_all()
        # Already stopped when stop_all returns, as deflake exits right after it
        assert started[0].wait(timeout=0.2) is not None
    finally:
        execution.resume()


def test_no_process_starts_after_stop_all(tmp_path: Path) -> None:
    execution.stop_all()
    try:
        with pytest.raises(KeyboardInterrupt):
            execution.launch([sys.executable, "-c", "pass"], {}, tmp_path / "log")
    finally:
        execution.resume()


def test_a_job_number_deflake_was_started_with_is_not_passed_on(monkeypatch: pytest.MonkeyPatch) -> None:
    passed: dict[str, str] = {}

    def start(command: list[str], env: dict[str, str], log: Path) -> execution.Status:
        passed.update(env)
        log.touch()

        return execution.Status(0)

    class Runner:
        def command(self, path: str, workdir: Path) -> list[str]:
            return []

        def success(self, code: int) -> bool:
            return True

    # A test of a deflake --jobs run starts deflake again, as deflake's own tests do
    monkeypatch.setenv(execution.JOB_KEY, "2")
    monkeypatch.setattr(execution, "start", start)
    execution.run(cast(Adapter, Runner()), "test_file.py", 1, {})
    assert execution.JOB_KEY not in passed

    execution.run(cast(Adapter, Runner()), "test_file.py", 1, {execution.JOB_KEY: "1"})
    assert passed[execution.JOB_KEY] == "1"


def test_reused_descendant_pids_are_not_killed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    registry = tmp_path / "processes"
    execution.events.append(registry, pid=123, identity="original-birth")
    execution.events.append(registry, pid=124, identity="matching-birth")
    monkeypatch.setattr(execution.events, "process_identity", lambda pid: "matching-birth")
    monkeypatch.setattr(os, "getpgid", lambda pid: 456)
    killed = []
    monkeypatch.setattr(execution, "signal_owned", lambda pid, identity, number: killed.append(pid))
    execution.stop_descendants(registry)
    assert killed == [124]


def test_descendants_registered_during_cleanup_are_also_stopped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    registry = tmp_path / "processes"
    execution.events.append(registry, pid=123, identity="same-birth")
    monkeypatch.setattr(execution.events, "process_identity", lambda pid: "same-birth")
    monkeypatch.setattr(execution, "group_targets", lambda pid: [])
    monkeypatch.setattr(os, "getpgid", lambda pid: pid)
    groups = []
    monkeypatch.setattr(os, "killpg", lambda pid, number: groups.append(pid))
    killed = []

    def kill(pid, identity, number):
        # A leader of a group is stopped before it is killed
        if number != signal.SIGKILL:
            return

        killed.append(pid)
        if pid == 123:
            execution.events.append(registry, pid=124, identity="same-birth")

    monkeypatch.setattr(execution, "signal_owned", kill)
    execution.stop_descendants(registry)
    assert killed == [123, 124]
    assert groups == []


def started(tmp_path: Path, handler: str) -> subprocess.Popen[bytes]:
    """A test process in a group of its own, as deflake starts them, once its SIGINT handler is set"""
    script = (
        "import signal, time\n"
        f"signal.signal(signal.SIGINT, {handler})\n"
        "open('ready', 'w').close()\n"
        "time.sleep(1.5)\n"
    )
    process = subprocess.Popen([sys.executable, "-c", script], cwd=tmp_path, start_new_session=True)
    deadline = time.monotonic() + 10
    while not (tmp_path / "ready").exists() and time.monotonic() < deadline:
        time.sleep(0.05)

    assert (tmp_path / "ready").exists(), "the process never set its handler"

    return process


def test_a_process_ignoring_the_interrupt_is_killed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(execution, "STOP_WAIT", 0.2)
    process = started(tmp_path, "signal.SIG_IGN")
    execution.interrupt([process])
    assert process.wait(timeout=0.5) == -signal.SIGKILL


def test_a_process_is_interrupted_once(tmp_path: Path) -> None:
    process = started(tmp_path, "lambda *_: open('signals', 'a').write('.')")
    # A worker that failed and the main thread stop the processes at the same time
    threads = [threading.Thread(target=execution.interrupt, args=([process],)) for _ in range(2)]
    for thread in threads:
        thread.start()

    for thread in threads:
        thread.join()

    # A second SIGINT would interrupt the teardown of its tests
    assert (tmp_path / "signals").read_text() == "."
    assert process.returncode == 0


def test_a_second_interrupt_kills_at_once(tmp_path: Path) -> None:
    process = started(tmp_path, "signal.SIG_IGN")
    # A real signal: from Python 3.15 a simulated one does not end a wait for a process
    timer = threading.Timer(0.2, os.kill, (os.getpid(), signal.SIGINT))
    timer.start()
    begun = time.monotonic()
    try:
        with pytest.raises(KeyboardInterrupt):
            execution.interrupt([process])
    finally:
        # A signal sent after the test would interrupt the whole session
        timer.cancel()

    assert process.wait(timeout=0.5) == -signal.SIGKILL
    assert time.monotonic() - begun < execution.STOP_WAIT


def test_a_second_interrupt_while_processes_start_kills_the_running_ones(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = started(tmp_path, "signal.SIG_IGN")
    # One more process is being started, which stop_all waits for
    monkeypatch.setattr(execution, "_launching", 1)
    monkeypatch.setattr(execution, "_running", {process})
    # A real signal: a simulated one does not wake a waiting lock
    timer = threading.Timer(0.2, os.kill, (os.getpid(), signal.SIGINT))
    timer.start()
    try:
        with pytest.raises(KeyboardInterrupt):
            execution.stop_all()
    finally:
        # A signal sent after the test would interrupt the whole session
        timer.cancel()
        execution.resume()

    assert process.wait(timeout=0.5) == -signal.SIGKILL


def test_every_kill_path_cleans_its_detached_descendants(tmp_path, monkeypatch):
    class Process:
        pid = 123

        def kill(self):
            pass

    # A stand-in with only what this path of execution uses
    process = cast(subprocess.Popen[bytes], Process())
    registry = tmp_path / "processes"
    monkeypatch.setattr(execution, "_descendants", {process: registry})
    cleaned = []
    monkeypatch.setattr(os, "killpg", lambda pid, number: None)
    monkeypatch.setattr(execution, "stop_descendants", cleaned.append)
    monkeypatch.setattr(execution, "targets_of", lambda process: [])
    execution.kill(process)
    assert cleaned == [registry]


def test_pid_reuse_between_check_and_signal_never_uses_numeric_signals(tmp_path, monkeypatch):
    registry = tmp_path / "processes"
    execution.events.append(registry, pid=123, identity="owned")
    monkeypatch.setattr(execution, "leads_group", lambda pid: False)
    monkeypatch.setattr(execution.sys, "platform", "linux")
    generation = {"current": 1}
    handles = {}
    checks = []
    delivered = []
    closed = []

    def identity(pid):
        checks.append(pid)
        # The process the pidfd was opened for exits, and its pid goes to another
        if handles:
            generation["current"] = 2

        return "owned"

    def open_pid(pid):
        handles[42] = generation["current"]

        return 42

    monkeypatch.setattr(execution.events, "process_identity", identity)
    monkeypatch.setattr(os, "pidfd_open", open_pid, raising=False)
    monkeypatch.setattr(
        signal,
        "pidfd_send_signal",
        lambda fd, number: delivered.append(handles[fd]),
        raising=False,
    )
    monkeypatch.setattr(os, "close", closed.append)
    signals = []
    monkeypatch.setattr(os, "kill", lambda *args: signals.append(args))
    monkeypatch.setattr(os, "killpg", lambda *args: signals.append(args))
    execution.stop_descendants(registry)
    assert signals == []
    assert delivered == [1]
    assert generation["current"] == 2
    assert closed == [42]


def test_linux_pidfd_keeps_original_target_when_pid_is_reused(monkeypatch):
    monkeypatch.setattr(execution.sys, "platform", "linux")
    generation = {"current": 1}
    handles = {}
    closed = []
    delivered = []

    def open_pid(pid):
        handles[42] = generation["current"]

        return 42

    def identity(pid):
        generation["current"] = 2

        return "original"

    monkeypatch.setattr(os, "pidfd_open", open_pid, raising=False)
    monkeypatch.setattr(execution.events, "process_identity", identity)
    monkeypatch.setattr(
        signal,
        "pidfd_send_signal",
        lambda fd, number: delivered.append(handles[fd]),
        raising=False,
    )
    monkeypatch.setattr(os, "close", closed.append)
    execution.signal_owned(123, "original", signal.SIGKILL)
    assert delivered == [1]
    assert generation["current"] == 2
    assert closed == [42]


def test_linux_pidfd_is_closed_without_signalling_a_reused_pid(monkeypatch):
    monkeypatch.setattr(execution.sys, "platform", "linux")
    monkeypatch.setattr(os, "pidfd_open", lambda pid: 42, raising=False)
    monkeypatch.setattr(execution.events, "process_identity", lambda pid: "replacement")
    closed = []
    signals = []
    monkeypatch.setattr(os, "close", closed.append)
    monkeypatch.setattr(signal, "pidfd_send_signal", lambda *args: signals.append(args), raising=False)
    execution.signal_owned(123, "original", signal.SIGKILL)
    assert signals == []
    assert closed == [42]


def test_reaped_leader_never_authorizes_a_new_group_snapshot(monkeypatch):
    class Process:
        pid = 123
        returncode = 0
        _waitpid_lock = threading.Lock()

    # A stand-in with only what this path of execution uses
    process = cast(subprocess.Popen[bytes], Process())
    monkeypatch.setattr(execution, "_members", {process: [(123, "original")]})

    def unexpected(pid):
        raise AssertionError("reaped PID must not be used to discover group members")

    monkeypatch.setattr(execution, "group_targets", unexpected)
    assert execution.targets_of(process) == [(123, "original")]


def test_missing_pidfd_support_rejects_launch_without_fallback(monkeypatch, tmp_path):
    monkeypatch.setattr(execution.sys, "platform", "linux")

    def unavailable(pid):
        raise OSError("pidfd unavailable")

    monkeypatch.setattr(os, "pidfd_open", unavailable, raising=False)
    with pytest.raises(execution.Error, match="safe process signalling"):
        execution.launch(["never-executed"], {}, tmp_path / "log")

    assert not (tmp_path / "log").exists()


def test_pidfd_is_closed_when_kernel_rejects_signal(monkeypatch):
    monkeypatch.setattr(execution.sys, "platform", "linux")
    monkeypatch.setattr(os, "pidfd_open", lambda pid: 42, raising=False)
    monkeypatch.setattr(execution.events, "process_identity", lambda pid: "original")

    def gone(*args):
        raise ProcessLookupError()

    monkeypatch.setattr(signal, "pidfd_send_signal", gone, raising=False)
    closed = []
    monkeypatch.setattr(os, "close", closed.append)
    execution.signal_owned(123, "original", signal.SIGKILL)
    assert closed == [42]


def test_reaped_process_kill_and_interrupt_never_signal_its_reused_group(monkeypatch):
    class Process:
        pid = 123
        returncode = 0
        _waitpid_lock = threading.Lock()

        def wait(self, timeout=None):
            return 0

    # A stand-in with only what this path of execution uses
    process = cast(subprocess.Popen[bytes], Process())
    monkeypatch.setattr(execution, "_members", {process: [(123, "old-generation")]})
    monkeypatch.setattr(execution.events, "process_identity", lambda pid: "new-generation")
    monkeypatch.setattr(execution.events, "audit_token", lambda pid: None)
    intents = []
    monkeypatch.setattr(os, "kill", lambda *args: intents.append(args))
    monkeypatch.setattr(os, "killpg", lambda *args: intents.append(args))
    execution.kill(process)
    execution.interrupt([process])
    assert intents == []


def test_numeric_signal_regression_is_isolated_from_native_apis(tmp_path, monkeypatch):
    monkeypatch.setattr(execution.sys, "platform", "linux")
    calls = []
    monkeypatch.setattr(os, "pidfd_open", lambda pid: calls.append(pid) or 42, raising=False)
    monkeypatch.setattr(signal, "pidfd_send_signal", lambda *args: None, raising=False)
    monkeypatch.setattr(os, "close", lambda fd: None)
    with pytest.MonkeyPatch.context() as inner:
        test_pid_reuse_between_check_and_signal_never_uses_numeric_signals(tmp_path, inner)

    assert calls == []


@pytest.mark.parametrize("replacement", [False, True])
def test_exec_during_audit_signal_reacquires_token_without_signalling_replacement(monkeypatch, replacement):
    import ctypes
    import errno
    import types

    monkeypatch.setattr(execution.sys, "platform", "darwin")
    state = {"version": 1, "birth": "original"}
    attempts = []
    delivered = []

    def token(pid):
        data = (ctypes.c_uint * 8)()
        data[5] = pid
        data[7] = state["version"]

        return bytes(data)

    def send(pointer, number):
        version = pointer._obj[7]
        attempts.append(version)
        if len(attempts) == 1:
            state["version"] = 2
            if replacement:
                state["birth"] = "replacement"
            return errno.ESRCH

        if version != state["version"]:
            return errno.ESRCH

        delivered.append((state["birth"], version))

        return 0

    monkeypatch.setattr(execution.events, "audit_token", token)
    monkeypatch.setattr(execution.events, "process_identity", lambda pid: state["birth"])
    monkeypatch.setattr(
        execution.ctypes,
        "CDLL",
        lambda *args: types.SimpleNamespace(proc_signal_with_audittoken=send),
    )
    execution.signal_owned(123, "original", signal.SIGKILL)
    assert delivered == ([] if replacement else [("original", 2)])
    assert attempts == ([1] if replacement else [1, 2])


@pytest.mark.parametrize("failure", [3, 1])
def test_audit_signal_failure_is_reported_instead_of_silent_cleanup(monkeypatch, failure):
    import ctypes
    import types

    monkeypatch.setattr(execution.sys, "platform", "darwin")
    token = (ctypes.c_uint * 8)()
    token[5] = 123
    monkeypatch.setattr(execution.events, "audit_token", lambda pid: bytes(token))
    monkeypatch.setattr(execution.events, "process_identity", lambda pid: "original")
    monkeypatch.setattr(execution, "STOP_WAIT", 0)
    monkeypatch.setattr(
        execution.ctypes,
        "CDLL",
        lambda *args: types.SimpleNamespace(proc_signal_with_audittoken=lambda *args: failure),
    )
    with pytest.raises(execution.Error, match="could not signal owned process"):
        execution.signal_owned(123, "original", signal.SIGKILL)


@pytest.mark.parametrize("disappear_on_read", [1, 2])
def test_registry_removed_by_other_cleanup_is_harmless(tmp_path, monkeypatch, disappear_on_read):
    registry = tmp_path / "processes"
    execution.events.append(registry, pid=123, identity="owned")
    monkeypatch.setattr(execution, "leads_group", lambda pid: False)
    original_read = execution.result_file.read
    reads = []

    def read(path):
        reads.append(path)
        if len(reads) == disappear_on_read:
            registry.unlink()

        return original_read(path)

    monkeypatch.setattr(execution.result_file, "read", read)
    monkeypatch.setattr(execution.events, "process_identity", lambda pid: "owned")
    signals = []
    monkeypatch.setattr(execution, "signal_owned", lambda *args: signals.append(args))
    execution.stop_descendants(registry)
    assert len(signals) == disappear_on_read - 1


@pytest.mark.parametrize(
    "code,outside,failed,valid",
    [
        (0, [], False, True),
        (1, [], True, True),
        (1, [], False, False),
        (2, [], True, False),
        (3, [], True, False),
        (4, [], True, False),
        (1, [{"outside": "module teardown failed"}], True, False),
        (0, [{"outside": "module teardown failed"}], False, False),
    ],
)
def test_causal_rows_require_a_valid_process_outcome(monkeypatch, code, outside, failed, valid):
    row = {"id": "victim", "passed": not failed}
    outcome = execution.Outcome([row], outside, execution.Status(code), code == 0, "")
    monkeypatch.setattr(execution, "run", lambda *args: outcome)
    result = execution.call(NO_ADAPTER, "test.py", 1)
    assert bool(result[0].get("process_interrupted")) is not valid


def test_abnormal_exit_is_not_explained_by_an_unfinished_other_test(monkeypatch):
    rows = [{"id": "victim", "passed": True}, {"id": "other", "passed": False, "group": "unfinished"}]
    outcome = execution.Outcome(rows, [], execution.Status(2), False, "")
    monkeypatch.setattr(execution, "run", lambda *args: outcome)
    assert all(row["process_interrupted"] for row in execution.call(NO_ADAPTER, "test.py", 1))


def test_interrupt_finishes_if_worker_removes_registry_during_read(tmp_path, monkeypatch):
    class Process:
        pid = 123
        returncode = 0
        _waitpid_lock = threading.Lock()

        def wait(self, timeout=None):
            return 0

    # A stand-in with only what this path of execution uses
    process = cast(subprocess.Popen[bytes], Process())
    registry = tmp_path / "processes"
    execution.events.append(registry, pid=123, identity="unused")
    original_read = execution.result_file.read

    def read(path):
        registry.unlink()

        return original_read(path)

    monkeypatch.setattr(execution.result_file, "read", read)
    monkeypatch.setattr(execution, "_members", {})
    monkeypatch.setattr(execution, "_descendants", {process: registry})

    def unexpected(*args):
        raise AssertionError("no live process belongs to this completed run")

    monkeypatch.setattr(execution, "signal_owned", unexpected)
    execution.interrupt([process])
    assert not registry.exists()


def test_a_group_is_stopped_before_any_of_it_is_killed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = tmp_path / "processes"
    execution.events.append(registry, pid=123, identity="leader")
    monkeypatch.setattr(
        execution.events,
        "process_identity",
        lambda pid: "leader" if pid == 123 else "member",
    )
    monkeypatch.setattr(execution, "leads_group", lambda pid: True)
    # A member forks another while the first pass stops it
    passes = iter([[(123, "leader"), (124, "member")], [(124, "member"), (125, "member")], []])
    monkeypatch.setattr(execution, "group_targets", lambda pid: next(passes))
    signals = []

    def record(pid, identity, number):
        signals.append((pid, number))
        if (pid, number) == (124, signal.SIGKILL):
            raise execution.Error("could not signal")

    monkeypatch.setattr(execution, "signal_owned", record)
    with pytest.raises(execution.Error):
        execution.stop_descendants(registry)

    stop, kill = signal.SIGSTOP, signal.SIGKILL
    assert signals == [(123, stop), (124, stop), (125, stop), (124, kill), (125, kill), (123, kill)]


def test_a_member_that_cannot_be_signalled_keeps_no_other_process_alive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = tmp_path / "processes"
    execution.events.append(registry, pid=123, identity="first")
    execution.events.append(registry, pid=200, identity="second")
    monkeypatch.setattr(execution.events, "process_identity", lambda pid: "first" if pid == 123 else "second")
    monkeypatch.setattr(execution, "leads_group", lambda pid: pid == 123)
    monkeypatch.setattr(execution, "group_targets", lambda pid: [(124, "second"), (125, "second")])
    stopped = []
    killed = []

    def record(pid, identity, number):
        # A member run by another user
        if pid == 124 and number == signal.SIGSTOP:
            raise execution.Error("could not signal owned process 124: Operation not permitted")

        if number == signal.SIGSTOP and pid != 123:
            stopped.append(pid)

        if number == signal.SIGKILL:
            killed.append(pid)

    monkeypatch.setattr(execution, "signal_owned", record)
    with pytest.raises(execution.Error, match="124"):
        execution.stop_descendants(registry)

    assert stopped == [125]
    assert killed == [124, 125, 123, 200]
