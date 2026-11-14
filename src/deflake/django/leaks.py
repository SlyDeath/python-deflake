"""Django's own global state a test changed and did not put back"""

from __future__ import annotations

import ast
import contextlib
import copy
import weakref
from collections import Counter
from typing import Any

from django.conf import settings
from django.core import signals as core_signals
from django.core.cache import caches
from django.db import connections
from django.db.models import signals as model_signals
from django.test import signals as test_signals
from django.test.signals import setting_changed
from django.urls import get_urlconf, set_urlconf
from django.utils import timezone, translation

from deflake import leaks
from deflake.django import databases
from deflake.leaks import states
from deflake.leaks.modules import fingerprint

# A setting that did not exist before
MISSING = object()


class Settings:
    def snapshot(self) -> dict[str, Any]:
        wrapped = settings._wrapped
        state = {name: fingerprint(getattr(wrapped, name, None), 0) for name in setting_names(wrapped)}

        return {"wrapped": id(wrapped), "values": state}

    def diff(self, before: dict[str, Any], after: dict[str, Any]) -> list[str]:
        old, new = before["values"], after["values"]
        # An override left enabled is reported by the settings it changed, so each can be tried as the cause
        messages = [
            f"settings.{name} was changed"
            for name in sorted(old.keys() | new.keys())
            if old.get(name) != new.get(name)
        ]
        if not messages and before["wrapped"] != after["wrapped"]:
            return ["override_settings is still active (enable() without disable()?)"]

        return messages


class Caches:
    """Keys added to a local memory cache; other backends live outside of the process"""

    def snapshot(self) -> dict[str, set[str]]:
        state = {}
        for alias in settings.CACHES:
            if settings.CACHES[alias]["BACKEND"] == "django.core.cache.backends.locmem.LocMemCache":
                # Connections are local to a context, but LocMemCache shares its store across threads
                cache = caches[alias]
            else:
                cache = getattr(caches._connections, alias, None)

            store = getattr(cache, "_cache", None)
            if isinstance(store, dict):
                state[alias] = set(store)

        return state

    def diff(self, before: dict[str, set[str]], after: dict[str, set[str]]) -> list[str]:
        messages = []
        for alias, keys in after.items():
            old = before.get(alias, set())
            added = keys - old
            if added:
                # Shorten only for display; versions and prefixes distinguish stored keys
                labels = {key: key.split(":", 2)[-1] for key in keys | old}
                counts = Counter(labels.values())
                names = [key if counts[labels[key]] > 1 else labels[key] for key in added]
                messages.append(f"cache {alias!r} has new keys: {', '.join(sorted(names))}")

        return messages


class Activated:
    """The language, time zone and URLconf a test activated for the rest of the thread"""

    def snapshot(self) -> dict[str, Any]:
        # Each part on its own: a project without one of the settings still has the others watched
        return {
            "the active language": leaks.quietly(translation.get_language),
            "the current time zone": leaks.quietly(timezone.get_current_timezone_name),
            "the URLconf of the thread": leaks.quietly(urlconf),
        }

    def diff(self, before: dict[str, Any], after: dict[str, Any]) -> list[str]:
        return [
            f"{name} was changed (activate() without deactivate()?)"
            for name in before
            if before[name] != after.get(name)
        ]


class State(states.Pieces):
    """What a probe puts back of Django: the activated language, time zone and URLconf, settings, the
    keys of local memory caches and the receivers of signals"""

    def read(self) -> dict[str, tuple[Any, Any]]:
        state: dict[str, tuple[Any, Any]] = {}
        with contextlib.suppress(Exception):
            state["the active language"] = states.same(translation.get_language())

        with contextlib.suppress(Exception):
            zone = timezone.get_current_timezone()
            state["the current time zone"] = (zone, str(zone))

        state["the URLconf of the thread"] = states.same(get_urlconf())

        wrapped = settings._wrapped
        for name in setting_names(wrapped):
            with contextlib.suppress(Exception):
                value = getattr(wrapped, name, None)
                # A copy, so a setting changed in place can be put back
                state[f"settings.{name}"] = (copied(value), fingerprint(value, 0))

        for alias, cache in local_caches().items():
            store, expiry = cache._cache, cache._expire_info
            state[f"cache {alias!r}"] = ((dict(store), dict(expiry)), set(store))

        for name, signal in SIGNALS.items():
            # Told apart as the leaks are, by the receivers still alive
            live = frozenset(Receivers.alive(signal))
            state[f"a receiver of {name}"] = (list(signal.receivers), live)

        return state

    def default(self, name: str) -> Any:
        if name.startswith("settings."):
            return MISSING

        return super().default(name)

    def write(self, name: str, value: Any) -> None:
        # Nothing activated reads as the default: deactivating keeps following the settings
        if name == "the active language":
            if not value:
                translation.deactivate_all()
            elif value == settings.LANGUAGE_CODE:
                translation.deactivate()
            else:
                translation.activate(value)
        elif name == "the current time zone":
            if str(value) == str(timezone.get_default_timezone()):
                timezone.deactivate()
            else:
                timezone.activate(value)
        elif name == "the URLconf of the thread":
            set_urlconf(value)
        elif name.startswith("settings."):
            setting = name.removeprefix("settings.")
            # Through the lazy settings, which keep every value read until it is set again
            if value is MISSING:
                delattr(settings, setting)
            else:
                setattr(settings, setting, value)
            # As override_settings does, so what caches a setting forgets it
            shown = None if value is MISSING else value
            setting_changed.send(sender=type(self), setting=setting, value=shown, enter=False)
        elif name.startswith("a receiver of "):
            signal = SIGNALS[name.removeprefix("a receiver of ")]
            with signal.lock:
                # A storage connects itself on first use, which the leaks leave out: it stays
                own = [receiver for receiver in signal.receivers if of_a_storage(receiver[1])]
                signal.receivers[:] = [*value, *(receiver for receiver in own if receiver not in value)]
                # The receivers of each sender are cached until the receivers change
                signal.sender_receivers_cache.clear()
        elif name.startswith("cache "):
            cache = local_caches()[ast.literal_eval(name.removeprefix("cache "))]
            store, expiry = value
            with cache._lock:
                cache._cache.clear()
                cache._cache.update(store)
                cache._expire_info.clear()
                cache._expire_info.update(expiry)
        else:
            raise ValueError(f"no state named {name}")


def setting_names(wrapped: Any) -> list[str]:
    # The holder of override_settings has no module of its own: that is not a setting changed
    return [name for name in dir(wrapped) if name.isupper() and name != "SETTINGS_MODULE"]


def copied(value: Any) -> Any:
    try:
        return copy.deepcopy(value)
    except Exception:
        # A value that cannot be copied is kept: only a change of it in place cannot be put back
        return value


def local_caches() -> dict[str, Any]:
    """A cache that cannot be made leaves the others to be put back"""
    result = {}
    for alias in settings.CACHES:
        if settings.CACHES[alias]["BACKEND"] == "django.core.cache.backends.locmem.LocMemCache":
            with contextlib.suppress(Exception):
                result[alias] = caches[alias]

    return result


def urlconf() -> str | None:
    # The test client sets the project's own until a streamed response is read
    current = get_urlconf()

    return None if current == getattr(settings, "ROOT_URLCONF", None) else current


def signals() -> dict[str, Any]:
    """Every signal of Django once, by the name of the module that defines it"""
    found: dict[int, tuple[str, Any]] = {}
    for module in (model_signals, core_signals, test_signals):
        for name, value in vars(module).items():
            if hasattr(value, "receivers"):
                found.setdefault(id(value), (f"{module.__name__}.{name}", value))

    return dict(found.values())


SIGNALS = signals()


class Receivers:
    """Receivers by their key, with the weak reference of one connected weakly"""

    def snapshot(self) -> dict[str, dict[Any, Any]]:
        return {name: self.alive(signal) for name, signal in SIGNALS.items()}

    @staticmethod
    def alive(signal: Any) -> dict[Any, Any]:
        return {
            receiver[0]: receiver[1] if isinstance(receiver[1], weakref.ReferenceType) else None
            for receiver in signal.receivers
            if not dead(receiver[1]) and not of_a_storage(receiver[1])
        }

    def diff(self, before: dict[str, dict[Any, Any]], after: dict[str, dict[Any, Any]]) -> list[str]:
        messages = []
        for name, receivers in after.items():
            old = before.get(name, {})
            if [key for key in receivers if key not in old]:
                messages.append(f"a receiver of {name} was connected and not disconnected")
            # One connected weakly whose object is gone since is dropped by Django, not disconnected
            if [key for key in old if key not in receivers and not dead(old[key])]:
                messages.append(f"a receiver of {name} was disconnected and not connected again")

        return messages


def dead(receiver: Any) -> bool:
    """A receiver connected weakly whose function is gone, which Django drops on the next send"""
    return isinstance(receiver, weakref.ReferenceType) and receiver() is None


def of_a_storage(receiver: Any) -> bool:
    """A file storage of Django connects itself on first use, to forget its paths when the settings
    change: Django's own doing, not the test's"""
    if not isinstance(receiver, weakref.ReferenceType):
        return False

    method = receiver()

    return getattr(getattr(method, "__func__", None), "__qualname__", None) == STORAGE_RECEIVER


STORAGE_RECEIVER = "StorageSettingsMixin._clear_cached_properties"


class Rows:
    """Rows created or deleted and not rolled back, in every test database. A database a test may not
    use, as with a SimpleTestCase, is not checked for it"""

    # The test databases are destroyed at the end of the session
    ends_with_session = True

    def snapshot(self) -> dict[str, int] | None:
        counts: dict[str, int] = {}
        with databases.access():
            for alias in databases.test_aliases():
                counts.update(count(alias))

        return counts

    def diff(self, before: dict[str, int], after: dict[str, int]) -> list[str]:
        messages = []
        for key in sorted(before.keys() & after.keys()):
            alias, table = key.split(".", 1)
            where = table if alias == "default" else f"{table} ({alias} database)"
            change = after[key] - before[key]
            if change > 0:
                messages.append(f"rows were created in {where} ({change})")
            elif change < 0:
                messages.append(f"rows were deleted from {where} ({-change})")

        return messages


def count(alias: str) -> dict[str, int]:
    """Nothing for a database the test may not use, so the others are still counted"""
    connection = connections[alias]
    # A connection deflake opens is closed again, so nothing holds the test database at its end
    opened = connection.connection is None
    counts = {}
    try:
        with connection.cursor() as cursor:
            for table in connection.introspection.django_table_names(only_existing=True):
                cursor.execute(f"SELECT COUNT(*) FROM {connection.ops.quote_name(table)}")
                counts[f"{alias}.{table}"] = cursor.fetchone()[0]
    except Exception:
        return {}
    finally:
        if opened and not in_memory(connection):
            with contextlib.suppress(Exception):
                connection.close()

    return counts


def in_memory(connection: Any) -> bool:
    """Closing an in-memory SQLite database would throw it away"""
    return bool(connection.vendor == "sqlite" and connection.is_in_memory_db())


def install() -> None:
    """Adds Django's detectors to the others, once"""
    if any(isinstance(detector, Settings) for detector in leaks.detectors):
        return

    # Django fills in the defaults of DATABASES on first use: not a test's change
    connections.settings  # noqa: B018
    databases.remember()
    leaks.detectors.extend([Settings(), Caches(), Activated(), Receivers(), Rows()])
    states.kinds["django"] = State
