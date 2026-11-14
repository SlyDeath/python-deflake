"""Global state a test changed and did not put back.

A detector is any object with two methods: snapshot() returns the current state, or None when the
detector does not apply, and diff(before, after) returns one message per leak. Add your own in a
conftest.py; detectors run only in the test processes deflake starts:

    from deflake import leaks

    leaks.detectors.append(MyDetector())
"""

from __future__ import annotations

import re
from typing import Any, Protocol

from deflake.leaks.clock import Clock
from deflake.leaks.env import Env
from deflake.leaks.logs import Logging
from deflake.leaks.modules import Modules
from deflake.leaks.patches import Patches
from deflake.leaks.process import Cwd, Settings, Signals, Sys
from deflake.leaks.threads import Threads
from deflake.leaks.warning_filters import WarningFilters


class Detector(Protocol):
    def snapshot(self) -> Any: ...

    def diff(self, before: Any, after: Any) -> list[str]: ...


Snapshot = list[tuple[Detector, Any]]

detectors: list[Detector] = [
    Env(),
    Cwd(),
    Clock(),
    Modules(),
    Patches(),
    Sys(),
    Logging(),
    WarningFilters(),
    Threads(),
    Settings(),
    Signals(),
]


# Kinds of detectors to leave out, such as one too slow for a project; Django's are added later, so
# leaving one out by its kind works wherever it is said
excluded: list[type] = []


def snapshot() -> Snapshot:
    chosen = [detector for detector in list(detectors) if not isinstance(detector, tuple(excluded))]

    return [(detector, quietly(detector.snapshot)) for detector in chosen]


def current() -> dict[int, Any]:
    """The state of every detector now, by its id"""
    return {id(detector): state for detector, state in snapshot()}


def since(before: Snapshot, taken: dict[int, Any] | None = None) -> list[str]:
    """Messages about what changed since the snapshot, up to now or up to the state already taken
    for a detector, by its id"""
    messages: list[str] = []
    for detector, old in before:
        if old is None:
            continue
        new = taken[id(detector)] if taken and id(detector) in taken else quietly(detector.snapshot)
        if new is None:
            continue
        messages.extend(quietly(lambda: detector.diff(old, new)) or [])  # noqa: B023

    return messages


def session_state(before: Snapshot) -> dict[int, Any]:
    """The state of the detectors that watch what the end of the test session takes away, such as
    the test databases, by the id of the detector"""
    return {
        id(detector): quietly(detector.snapshot)
        for detector, old in before
        if old is not None and getattr(detector, "ends_with_session", False)
    }


def own(leaked: list[str], of_tests: list[str]) -> list[str]:
    """Leaks of a group of tests that are not just its tests' leaks added up: "2 threads" after two
    tests that each left "1 thread" is the same leak"""
    kinds = {kind(leak) for leak in of_tests}

    return [leak for leak in leaked if kind(leak) not in kinds]


def kind(leak: str) -> str:
    """What a leak is about, without its count and the list after a colon"""
    leak = re.sub(r":\s.*\Z", "", leak, flags=re.DOTALL)

    return re.sub(r"\b\d+\b", "N", leak)


def quietly(call: Any) -> Any:
    """A broken detector must never break the tests it watches"""
    try:
        return call()
    except Exception:
        return None
