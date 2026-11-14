import sys
import threading
import time
from typing import Any

import pytest

from deflake import execution, repeat


def test_another_interrupt_still_waits_for_the_test_processes(monkeypatch: pytest.MonkeyPatch) -> None:
    interrupts = {"left": 2}

    class Event(threading.Event):
        def wait(self, timeout: float | None = None) -> bool:
            # The first Ctrl-C while the jobs run, the second while their processes stop
            if interrupts["left"] and sys._getframe(1).f_code.co_name == "in_parallel":
                interrupts["left"] -= 1
                raise KeyboardInterrupt

            return super().wait(timeout)

    def work(item: int, job: int) -> list[Any]:
        return []

    stops: list[int] = []
    monkeypatch.setattr(threading, "Event", Event)
    monkeypatch.setattr(execution, "stop_all", lambda: stops.append(1))
    with pytest.raises(KeyboardInterrupt):
        repeat.in_parallel([1], 1, work)

    assert (len(stops), interrupts["left"]) == (2, 0)


def test_an_interrupt_while_processes_start_still_waits_for_them(monkeypatch: pytest.MonkeyPatch) -> None:
    interrupted = threading.Event()
    finished = []

    def work(item: int, job: int) -> list[Any]:
        # The job ends only after Ctrl-C, so leaving without waiting would miss it
        interrupted.wait()
        time.sleep(0.2)
        finished.append(item)

        return []

    class Event(threading.Event):
        def wait(self, timeout: float | None = None) -> bool:
            if sys._getframe(1).f_code.co_name == "in_parallel" and not interrupted.is_set():
                interrupted.set()
                raise KeyboardInterrupt

            return super().wait(timeout)

    def stop_all() -> None:
        # As when another Ctrl-C comes while processes are being started
        raise KeyboardInterrupt

    monkeypatch.setattr(threading, "Event", Event)
    monkeypatch.setattr(execution, "stop_all", stop_all)
    with pytest.raises(KeyboardInterrupt):
        repeat.in_parallel([1], 1, work)

    assert finished == [1]


def test_an_interrupt_that_broke_the_lock_of_a_wait_is_an_interrupt(monkeypatch: pytest.MonkeyPatch) -> None:
    broken = {"left": 1}

    class Event(threading.Event):
        def wait(self, timeout: float | None = None) -> bool:
            if broken["left"] and sys._getframe(1).f_code.co_name == "in_parallel":
                broken["left"] -= 1
                raise RuntimeError("release unlocked lock")

            return super().wait(timeout)

    def work(item: int, job: int) -> list[Any]:
        return []

    monkeypatch.setattr(threading, "Event", Event)
    monkeypatch.setattr(execution, "stop_all", lambda: None)
    with pytest.raises(KeyboardInterrupt):
        repeat.in_parallel([1], 1, work)
