"""State a probe can put back after a culprit and set again, to prove it is what breaks the victim.

A kind has snapshot() of restorable values by name, label(name) as its leaks name the state,
changed(before, after) and restore(name, value, exists=). Its keys reach the report as "kind:name"
"""

from __future__ import annotations

import ast
import contextlib
import copyreg
import decimal
import logging
import os
import re
import signal
import socket
import sys
import uuid
import warnings
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from deflake.leaks.env import Env
from deflake.leaks.modules import Scalars
from deflake.leaks.patches import describe
from deflake.leaks.process import SIGNALS, context_state, main_thread
from deflake.leaks.warning_filters import filters_state


class Kind(Protocol):
    def snapshot(self) -> dict[str, Any]: ...

    def label(self, name: str) -> str: ...

    def changed(self, before: dict[str, Any], after: dict[str, Any]) -> list[str]: ...

    def restore(self, name: str, value: Any, *, exists: bool = True) -> None: ...


class Pieces:
    """Named pieces of state, each read as (value, what tells it apart) and written back"""

    def read(self) -> dict[str, tuple[Any, Any]]:
        raise NotImplementedError

    def write(self, name: str, value: Any) -> None:
        raise NotImplementedError

    def default(self, name: str) -> Any:
        """The value of a piece that did not exist before"""
        raise ValueError(f"{name} did not exist before")

    def snapshot(self) -> dict[str, Any]:
        return self.read()

    @staticmethod
    def label(name: str) -> str:
        return name

    @staticmethod
    def changed(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
        return sorted(
            name
            for name in before.keys() | after.keys()
            if (name in before) != (name in after) or before[name][1] != after[name][1]
        )

    def restore(self, name: str, value: Any, *, exists: bool = True) -> None:
        self.write(name, value[0] if exists else self.default(name))


class Process(Pieces):
    """The working directory, sys, socket and decimal settings, signal handlers, warnings and logging"""

    def read(self) -> dict[str, tuple[Any, Any]]:
        state: dict[str, tuple[Any, Any]] = {}
        with contextlib.suppress(OSError):
            cwd = str(Path.cwd())
            state["the working directory"] = (cwd, cwd)

        state["sys.path"] = (list(sys.path), list(sys.path))
        state["sys.getrecursionlimit()"] = same(sys.getrecursionlimit())
        state["socket.getdefaulttimeout()"] = same(socket.getdefaulttimeout())
        context = decimal.getcontext()
        state["decimal.getcontext()"] = (context.copy(), context_state(context))
        state["warnings.filters"] = (list(warnings.filters), filters_state(warnings.filters))
        if main_thread():
            for number in SIGNALS:
                handler = signal.getsignal(number)
                state[f"the handler of {signal.Signals(number).name}"] = (handler, id(handler))

        state["logging.disable()"] = same(logging.root.manager.disable)
        for name, logger in loggers().items():
            label = f"logger {name!r}" if name else "the root logger"
            handlers = list(logger.handlers)
            state[f"the level of {label}"] = same(logger.level)
            state[f"the handlers of {label}"] = (handlers, [id(handler) for handler in handlers])
            state[f"the propagate setting of {label}"] = same(logger.propagate)
            state[f"the disabled setting of {label}"] = same(logger.disabled)

        return state

    def default(self, name: str) -> Any:
        # A logger made by the culprit starts with the defaults
        for part, value in (
            ("level", logging.NOTSET),
            ("handlers", []),
            ("propagate", True),
            ("disabled", False),
        ):
            if name.startswith(f"the {part} ") and " of logger " in name:
                return value

        return super().default(name)

    def write(self, name: str, value: Any) -> None:
        writers: dict[str, Callable[[Any], None]] = {
            "the working directory": os.chdir,
            "sys.path": lambda paths: sys.path.__setitem__(slice(None), paths),
            "sys.getrecursionlimit()": sys.setrecursionlimit,
            "socket.getdefaulttimeout()": socket.setdefaulttimeout,
            "decimal.getcontext()": lambda context: decimal.setcontext(context.copy()),
            "warnings.filters": set_filters,
            "logging.disable()": logging.disable,
        }
        if name in writers:
            writers[name](value)
        elif name.startswith("the handler of "):
            signal.signal(signal.Signals[name.removeprefix("the handler of ")], value)
        else:
            set_logger(name, value)


def same(value: Any) -> tuple[Any, Any]:
    """A piece told apart by its value itself"""
    return (value, value)


def set_filters(filters: list[Any]) -> None:
    # typeshed declares the filters a Sequence; at runtime they are a list
    warnings.filters[:] = filters  # type: ignore[index]
    # Warnings already shown are cached per module until the filters change
    mutated = getattr(warnings, "_filters_mutated", None) or getattr(
        warnings,
        "_filters_mutated_lock_held",
        None,
    )
    if mutated is not None:
        mutated()


def loggers() -> dict[str, logging.Logger]:
    found: dict[str, logging.Logger] = {"": logging.getLogger()}
    found.update(
        (name, logger)
        for name, logger in list(logging.Logger.manager.loggerDict.items())
        if isinstance(logger, logging.Logger)
    )

    return found


def set_logger(name: str, value: Any) -> None:
    part, _, label = name.removeprefix("the ").partition(" of ")
    logger = (
        logging.getLogger()
        if label == "the root logger"
        else logging.getLogger(ast.literal_eval(label.removeprefix("logger ")))
    )
    if part == "level":
        logger.setLevel(value)
    elif part == "handlers":
        logger.handlers[:] = value
    elif part == "propagate setting":
        logger.propagate = value
    elif part == "disabled setting":
        logger.disabled = value
    else:
        raise ValueError(f"no state named {name}")


class Patched(Pieces):
    """unittest.mock patches started and not stopped, each put back by stopping it and set again with
    the very object it put in place, so what the culprit set on its mock stays"""

    def __init__(self) -> None:
        # The patches a repair stopped, with their place among the active ones, which a reapply sets
        # again whatever has their name now
        self.stopped: list[tuple[Any, int]] = []

    def read(self) -> dict[str, tuple[Any, Any]]:
        state: dict[str, tuple[Any, Any]] = {}
        for patch in list(mock_patches()):
            # One patch that cannot be read leaves the others to try
            try:
                stored = (patch, in_place(patch))
            except Exception:
                continue

            name = base = f"mock.patch of {describe(patch)}"
            # Patches of the same target are told apart by the order they started in
            number = 1
            while name in state:
                number += 1
                name = f"{base} #{number}"
            state[name] = (stored, id(patch))

        return state

    @staticmethod
    def label(name: str) -> str:
        return re.sub(r" #\d+$", "", name)

    def restore(self, name: str, value: Any, *, exists: bool = True) -> None:
        active = self.read()
        # A name tells patches apart by their order: another patch under it now is not the one to set
        if exists:
            mine = value[0][0]
            stopped = any(patch is mine for patch, _ in self.stopped)
            if stopped or name not in active or active[name][0][0] is mine:
                self.write(name, value[0])
                return

        if name not in active:
            raise ValueError(f"{name} is not active")

        # Stopping a patch puts back what was there when it started, undoing the later ones too
        patch = active[name][0][0]
        later = [other[0][0] for other in list(active.values())[list(active).index(name) + 1 :]]
        if any(targets(patch) & targets(other) for other in later):
            raise ValueError(f"{name} has another patch of its targets over it")

        # Taken before the patch leaves the active ones, for a reapply to put it back there
        place = mock_patches().index(patch)
        if hasattr(patch, "in_dict") and not patch.clear:
            # Only its own keys: the rest of the dict may have changed since, and is not the patch's
            for key in patch.values:
                if key in patch._original:
                    patch.in_dict[key] = patch._original[key]
                else:
                    patch.in_dict.pop(key, None)
            mock_patches().remove(patch)
        else:
            patch.stop()

        self.stopped.append((patch, place))

    def write(self, name: str, value: Any) -> None:
        patch, replacements = value

        # Started again, so stopall() and the patch's own stop() undo it as in the culprit's run
        place = next((place for stopped, place in self.stopped if stopped is patch), None)
        if place is not None:
            # A dict taken off by hand keeps what it was before the culprit, which stopall() puts back
            original = getattr(patch, "_original", None)
            patch.start()
            if hasattr(patch, "in_dict") and not patch.clear:
                patch._original = original
            # Back in its place, so patches are stopped and named in the order the culprit started them
            active = mock_patches()
            active.remove(patch)
            active.insert(place, patch)
            self.stopped = [(stopped, at) for stopped, at in self.stopped if stopped is not patch]

        if hasattr(patch, "in_dict"):
            if patch.clear:
                patch.in_dict.clear()
            # The keys of the patch the culprit deleted stay deleted, though starting it again sets them
            for key in patch.values:
                if key not in replacements:
                    patch.in_dict.pop(key, None)
            patch.in_dict.update(replacements)
        else:
            for target, attribute, replacement in replacements:
                setattr(target, attribute, replacement)


def targets(patch: Any) -> set[tuple[int, Any]]:
    """What a patch changes, by the object and the attribute or key"""
    if hasattr(patch, "in_dict"):
        keys = list(patch.in_dict) if patch.clear else list(patch.values)

        return {(id(patch.in_dict), key) for key in keys}

    patchers = [patch, *getattr(patch, "additional_patchers", [])]

    return {(id(patcher.getter()), patcher.attribute) for patcher in patchers}


def mock_patches() -> list[Any]:
    mock = sys.modules.get("unittest.mock")

    return getattr(getattr(mock, "_patch", None), "_active_patches", None) or []


def in_place(patch: Any) -> Any:
    """What a patch put in place: the patched keys of a dict, or each attribute as it is stored, so a
    staticmethod stays one, or as it reads where it is not stored, as on a proxy or in a slot"""
    if hasattr(patch, "in_dict"):
        keys = patch.in_dict if patch.clear else patch.values

        return {key: patch.in_dict[key] for key in keys if key in patch.in_dict}

    result = []
    patchers = [patch, *getattr(patch, "additional_patchers", [])]
    for patcher in patchers:
        target = patcher.getter()
        attribute = patcher.attribute
        stored = getattr(target, "__dict__", {})
        value = stored[attribute] if attribute in stored else getattr(target, attribute)
        result.append((target, attribute, value))

    return result


class Frozen(Pieces):
    """Time freezegun froze and did not let go: put back as freezegun stops the last freezer, set again
    by freezing it with the culprit's own clocks, moved or ticked as they were. The freezer itself is
    often gone, as with freeze_time(...).start() kept nowhere"""

    def read(self) -> dict[str, tuple[Any, Any]]:
        api = sys.modules.get("freezegun.api")
        factories = list(getattr(api, "freeze_factories", None) or [])
        if api is None or not factories:
            return {}

        stacks = [list(getattr(api, name)) for name in STACKS]

        return {"freezegun": (stacks, [id(factory) for factory in factories])}

    def restore(self, name: str, value: Any, *, exists: bool = True) -> None:
        if exists:
            self.write(name, value[0])
            return

        api = sys.modules["freezegun.api"]
        for stack in STACKS:
            getattr(api, stack).clear()

        # Every fake back to the real one, wherever a module took it, but in freezegun's own
        reals = {id(fake): real for fake, real in fakes(api)}
        for module_name, module in list(sys.modules.items()):
            if module_name.split(".")[0] == "freezegun":
                continue
            for attribute, found in list(getattr(module, "__dict__", {}).items()):
                if id(found) in reals:
                    with contextlib.suppress(Exception):
                        setattr(module, attribute, reals[id(found)])

        copyreg.dispatch_table.pop(api.real_datetime, None)
        copyreg.dispatch_table.pop(api.real_date, None)

        if api.uuid_generate_time_attr:
            setattr(uuid, api.uuid_generate_time_attr, api.real_uuid_generate_time)

        uuid._UuidCreate = api.real_uuid_create  # type: ignore[attr-defined]
        uuid._last_timestamp = None  # type: ignore[attr-defined]

    def write(self, name: str, value: Any) -> None:
        api = sys.modules["freezegun.api"]
        if not api.freeze_factories:
            # Any freezer changes the modules; the culprit's clocks replace its own
            api.freeze_time("2000-01-01").start()

        for stack, values in zip(STACKS, value, strict=True):
            getattr(api, stack)[:] = values


# What freezegun keeps for every freezer started, the clocks first
STACKS = ("freeze_factories", "tz_offsets", "ignore_lists", "tick_flags")


def fakes(api: Any) -> list[tuple[Any, Any]]:
    """The fakes of freezegun with the real functions and classes they stand in for"""
    pairs = [(api.FakeDate, api.real_date), (api.FakeDatetime, api.real_datetime)]
    for name in ("time", "localtime", "gmtime", "monotonic", "perf_counter", "strftime", "clock"):
        for suffix in ("", "_ns"):
            fake = getattr(api, f"fake_{name}{suffix}", None)
            real = getattr(api, f"real_{name}{suffix}", None)
            if fake is not None and real is not None:
                pairs.append((fake, real))

    return pairs


# Kinds by the name the report shows; Django adds its own
kinds: dict[str, Callable[[], Kind]] = {
    "environment": Env,
    "module": Scalars,
    "process": Process,
    "patch": Patched,
    "clock": Frozen,
}


# The kinds whose snapshot failed, under a key no kind has
FAILED = "\0failed"


class Probed:
    """Every kind at once, with keys "kind:name", so a culprit is measured in one pair of runs"""

    def __init__(self) -> None:
        self.parts: dict[str, Kind] | None = None

    def kinds(self) -> dict[str, Kind]:
        # Made on first use: Django adds its kind once the runner has set it up
        if self.parts is None:
            self.parts = {name: make() for name, make in kinds.items()}

        return self.parts

    def snapshot(self) -> dict[str, Any]:
        failed: set[str] = set()
        state: dict[str, Any] = {}
        for kind, part in self.kinds().items():
            try:
                taken = part.snapshot()
            except Exception:
                failed.add(kind)
                continue
            state.update((f"{kind}:{name}", value) for name, value in taken.items())

        state[FAILED] = failed

        return state

    def label(self, key: str) -> str:
        kind, _, name = key.partition(":")

        return self.kinds()[kind].label(name)

    def changed(self, before: dict[str, Any], after: dict[str, Any]) -> list[str]:
        result: list[str] = []
        for kind, part in self.kinds().items():
            prefix = f"{kind}:"
            old = {key[len(prefix) :]: value for key, value in before.items() if key.startswith(prefix)}
            new = {key[len(prefix) :]: value for key, value in after.items() if key.startswith(prefix)}
            # A kind whose snapshot failed on one side tells nothing of what changed
            if kind not in before.get(FAILED, set()) | after.get(FAILED, set()):
                result.extend(prefix + name for name in part.changed(old, new))

        return result

    def restore(self, key: str, value: Any, *, exists: bool = True) -> None:
        kind, _, name = key.partition(":")
        self.kinds()[kind].restore(name, value, exists=exists)
