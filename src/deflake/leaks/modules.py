"""Global variables of the project's own modules, assigned or changed in place.

Data is compared by value: numbers, strings, dates and the like, lists, tuples, dicts and sets of
them, and objects of the project's own classes by their attributes. An object of an installed
library counts by identity: what a client, a logger or a proxy sets up inside on its own use is not
a leak, and replacing it with another object is
"""

from __future__ import annotations

import collections
import datetime
import decimal
import enum
import fractions
import functools
import hashlib
import io
import itertools
import logging
import pathlib
import pickle
import random
import re
import sys
import sysconfig
import uuid
import warnings
from pathlib import Path
from types import ModuleType
from typing import Any, ClassVar
from weakref import ReferenceType, WeakValueDictionary, ref

# Where the tests run from, taken before any test can change it
ROOT = Path.cwd().resolve()
# Folders of installed packages, whose modules are not the project's
INSTALLED = {"site-packages", "dist-packages", ".venv", "venv", ".tox", ".nox"}
# How deep the parts of a value are compared; deeper ones count by identity
DEPTH = 8
# How many parts of one global are compared one by one: a larger one is compared as pickle dumps
# it, with the order of its keys and of the items of its sets
NODES = 50_000
# Values compared as they are
SCALARS = (
    type(None),
    bool,
    int,
    float,
    complex,
    str,
    bytes,
    bytearray,
    range,
    decimal.Decimal,
    fractions.Fraction,
    datetime.date,
    datetime.time,
    datetime.timedelta,
    datetime.tzinfo,
    uuid.UUID,
    pathlib.PurePath,
    re.Pattern,
)
SEQUENCES = (list, tuple, collections.deque)
SETS = (set, frozenset)
# Classes outside of the project whose objects are data, kept in their attributes
DATA = {
    ("pydantic", "BaseModel"),
    ("pydantic", "RootModel"),
    ("pydantic_settings", "BaseSettings"),
    ("types", "SimpleNamespace"),
    ("argparse", "Namespace"),
}
# Classes of builtins a class of the project may build on and stay data
BUILTIN = {object, dict, list, tuple, set, frozenset}
# Objects of the standard library that change on their own use: a random generator, which deflake
# seeds anyway, a decimal context, which keeps flags of the last operations, and a log handler or
# a connection, which opens its file or socket when first used
USED = (random.Random, decimal.Context, logging.Handler)
# Scalars hashed as they are
HASHED = {type(None), bool, int, str, bytes}
# Scalars a list, a dict or a set of which is compared at once, as no two of different types are
# equal: unlike True and 1, or 1 and 1.0
PLAIN = frozenset({type(None), int, str, bytes})
# Scalars compared at once with their type
TAGGED = PLAIN | {bool, float}
# What pydantic keeps about which fields were set: setting a field back leaves it there
SERVICE = {"__pydantic_fields_set__"}
# Modules of the standard library whose classes a class of the project may build on and stay data
STANDARD = {"builtins", "abc", "collections", "typing", "dataclasses", "enum", "_collections_abc"}


class Modules:
    # Shared with class classification, without keeping unloaded modules alive
    modules: ClassVar[WeakValueDictionary[int, ModuleType]] = WeakValueDictionary()

    def __init__(self) -> None:
        self.files: dict[str, bool] = {}

    def snapshot(self) -> dict[tuple[str, str], Any]:
        return {key: fingerprint(value, 0) for key, value in self.values(with_code=False).items()}

    def values(self, *, with_code: bool = True) -> dict[tuple[str, str], Any]:
        """The same watched attributes for leak detection and reversible scalar experiments. Code is
        left out before its key is made, as most attributes of a module are code"""
        state = {}
        for name, module in list(sys.modules.items()):
            if not self.own(module):
                continue
            for attribute, value in list(vars(module).items()):
                if attribute.startswith("__") and attribute != "__file__":
                    continue
                if not with_code and code(value):
                    continue
                state[(name, attribute)] = value

        return state

    def diff(self, before: dict[tuple[str, str], Any], after: dict[tuple[str, str], Any]) -> list[str]:
        modules = {module for module, _ in before}
        messages = []
        for key in sorted(before.keys() | after.keys()):
            if key[0] not in modules:
                continue
            label = ".".join(key)
            if key not in after:
                messages.append(f"{label} was removed")
            elif key not in before:
                messages.append(f"{label} was set")
            elif before[key] != after[key]:
                messages.append(f"{label} was changed")

        return messages

    def own(self, module: Any) -> bool:
        if module is not None and self.modules.get(id(module)) is module:
            return True

        file = getattr(module, "__file__", None)
        if not isinstance(file, str):
            return False

        if file not in self.files:
            self.files[file] = (
                project_file(file) and getattr(module, "__name__", "").split(".")[0] != "deflake"
            )

        if self.files[file] and isinstance(module, ModuleType):
            self.modules[id(module)] = module

        return self.files[file]


ownership = Modules()


def project_file(file: str) -> bool:
    """A file of the project, not of a package installed into it or of Python's own environment,
    which may be in the project's folder too, as with pixi or conda"""
    path = Path(file).resolve()

    return (
        path.is_relative_to(ROOT)
        and not INSTALLED & set(path.relative_to(ROOT).parts)
        and not any(path.is_relative_to(folder) for folder in environment(ROOT))
    )


@functools.cache
def environment(root: Path) -> tuple[Path, ...]:
    """Folders of the Python running the tests, but one the project is in"""
    paths = sysconfig.get_paths()
    folders = [paths.get(name) for name in ("stdlib", "platstdlib", "purelib", "platlib")]
    folders += [sys.prefix, sys.base_prefix, sys.exec_prefix, sys.base_exec_prefix]
    resolved = {Path(folder).resolve() for folder in folders if folder}

    return tuple(folder for folder in resolved if not root.is_relative_to(folder))


def code(value: Any) -> bool:
    """Modules, classes and functions, which are code, not state. Only the type of the value is
    looked at: a lazy proxy computes itself when asked anything else"""
    kind: Any = type(value)

    return issubclass(kind, type) or kind.__name__ in {
        "module",
        "function",
        "builtin_function_or_method",
        "method",
    }


class TooLarge(BaseException):
    """More parts in a global than are compared one by one. Not an Exception, so that the guards
    against failing looks inside let it through"""


def fingerprint(
    value: Any,
    depth: int,
    path: frozenset[int] = frozenset(),
    budget: list[int] | None = None,
) -> Any:
    """What a value is made of, as far as the project owns it. Only the type of a value decides how
    it is compared, so a proxy is never asked about itself"""
    if budget is None:
        try:
            return fingerprint(value, depth, path, [NODES])
        except TooLarge:
            return ("dump", dump(value))

    budget[0] -= 1
    if budget[0] < 0:
        raise TooLarge

    kind: Any = type(value)
    try:
        return compared(value, kind, depth, path, budget)
    except (TypeError, AttributeError):
        # A class that cannot be cached by, as with a metaclass that has __eq__ and no __hash__
        return ("id", id(value))


def compared(value: Any, kind: Any, depth: int, path: frozenset[int], budget: list[int]) -> Any:
    if issubclass(kind, SCALARS):
        return scalar(value, kind)
    if issubclass(kind, enum.Enum):
        return ("enum", kind.__qualname__, value.name)
    if kind.__name__ == "method" and code(value):
        return ("method", id(value.__self__), id(value.__func__))
    if depth >= DEPTH or id(value) in path or code(value):
        return ("id", id(value))

    if kind in (dict, list, tuple, set, frozenset):
        found = plain(value, kind, budget)
        if found is not None:
            return found

    if not data_class(kind):
        return standard_object(value, kind, depth, path, budget)

    path = path | {id(value)}
    try:
        parts = contents(value, kind, depth, path, budget)
        return (kind.__qualname__, *parts, *attributes(value, depth, path, budget))
    except Exception:
        # A container of the project may fail on any look inside: it still counts by identity
        return ("id", id(value))


def plain(value: Any, kind: Any, budget: list[int]) -> Any:
    """A container of scalars only, as a loaded table has, compared many times faster"""
    items: list[Any] = list(dict.items(value)) if kind is dict else list(value)
    found = {type(part) for part in (itertools.chain.from_iterable(items) if kind is dict else items)}
    if not found <= TAGGED:
        return None

    budget[0] -= len(items)
    if not found <= PLAIN:
        # True is 1 and 1.0, and nan is not itself: the type and the repr tell them apart
        items = (
            [tagged(item) for item in items]
            if kind is not dict
            else [(tagged(key), tagged(item)) for key, item in items]
        )

    return (kind.__qualname__, tuple(items) if kind in (list, tuple) else frozenset(items))


def tagged(value: Any) -> Any:
    return (type(value).__name__, repr(value) if type(value) is float else value)


def scalar(value: Any, kind: Any) -> Any:
    if issubclass(kind, (float, complex)):
        # nan is not equal to itself
        return (kind.__name__, repr(value))
    if issubclass(kind, bytearray):
        return (kind.__name__, bytes(value))

    if kind not in HASHED:
        try:
            hash(value)
        except TypeError:
            # As a time zone of dateutil, which has no hash
            return (kind.__name__, repr(value))

    return (kind.__name__, value)


def standard_object(value: Any, kind: Any, depth: int, path: frozenset[int], budget: list[int]) -> Any:
    """An object of the standard library is compared as pickle dumps it, an object of an installed
    library by identity"""
    if not standard(kind) or used(kind):
        return ("id", id(value))

    if isinstance(value, functools.partial):
        parts = (value.func, value.args, value.keywords)
        return ("partial", *(fingerprint(part, depth + 1, path, budget) for part in parts))

    if module_of(kind) == "itertools":
        # pickle dumps them with a warning on some versions of Python and not at all on others
        return ("itertools", repr(value))

    return ("standard", dump(value, strict=True))


def contents(value: Any, kind: type, depth: int, path: frozenset[int], budget: list[int]) -> tuple[Any, ...]:
    if issubclass(kind, dict):
        # A key removed and put back moves to the end of a dict, which is still the same dict
        return (
            frozenset(
                (fingerprint(key, depth + 1, path, budget), fingerprint(item, depth + 1, path, budget))
                for key, item in list(dict.items(value))
            ),
        )
    if issubclass(kind, SEQUENCES):
        return (tuple(fingerprint(item, depth + 1, path, budget) for item in list(value)),)
    if issubclass(kind, SETS):
        return (frozenset(fingerprint(item, depth + 1, path, budget) for item in list(value)),)

    return ()


class Dumper(pickle.Pickler):
    """pickle by the rules of the comparison part by part: an object of an installed library, or
    one changed on its own use, counts by identity, and an object of the project by its attributes
    less what it fills in on its own use. None of their own code runs.

    strict: an object of the standard library is dumped with its parts, and fails the dump when
    one of them cannot be dumped. Otherwise each is dumped strictly on its own, and counts by
    identity when that fails, as a thread pool with its queue or a connection of sqlite3
    """

    def __init__(self, file: io.BytesIO, *, strict: bool) -> None:
        super().__init__(file, protocol=pickle.HIGHEST_PROTOCOL)
        self.strict = strict

    def reducer_override(self, obj: Any) -> Any:
        kind: Any = type(obj)
        if obj is tuple:
            # What identity rebuilds its marks with
            return NotImplemented
        if code(obj) or used(kind):
            return identity(obj)
        if issubclass(kind, enum.Enum):
            return NotImplemented

        if data_class(kind):
            if kind in BUILTIN:
                return NotImplemented
            parts = list(obj) if isinstance(obj, (*SEQUENCES, *SETS)) else []
            items = list(dict.items(obj)) if isinstance(obj, dict) else []
            # What it holds goes as the state, after the object is remembered, so a cycle ends
            return (tuple, ((kind.__qualname__,),), (parts, items, own_state(obj, kind)))

        if not standard(kind):
            return identity(obj)
        if module_of(kind) == "itertools":
            return (tuple, (("itertools", repr(obj)),))
        if self.strict or issubclass(kind, SCALARS):
            return NotImplemented

        reduced = scalar_reduce(obj)
        if reduced is not None:
            # Dumped as it is, many times faster than on its own, as an address of ipaddress
            return reduced

        return (tuple, (("standard", dump(obj, strict=True)),))


def scalar_reduce(obj: Any) -> Any:
    """How an object of the standard library is dumped, when it is made of scalars only"""
    try:
        # Any, as __reduce_ex__ of an arbitrary object returns whatever the class decides
        reduced: Any = obj.__reduce_ex__(pickle.HIGHEST_PROTOCOL)
    except Exception:
        return None

    if not isinstance(reduced, tuple) or not 2 <= len(reduced) <= 3 or not isinstance(reduced[1], tuple):
        return None

    parts = [*reduced[1], *(reduced[2:] or [None])]

    return reduced if all(type(part) in TAGGED for part in parts) else None


def identity(obj: Any) -> Any:
    return (tuple, (("id", id(obj)),))


def dump(value: Any, *, strict: bool = False) -> Any:
    try:
        buffer = io.BytesIO()
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            Dumper(buffer, strict=strict).dump(value)
        return hashlib.blake2b(buffer.getvalue(), digest_size=16).digest()
    except Exception:
        return ("id", id(value))


def attributes(value: Any, depth: int, path: frozenset[int], budget: list[int]) -> tuple[Any, ...]:
    """Attributes of an object of the project, less what it fills in on its own use"""
    state = own_state(value, type(value))
    if not state:
        return ()

    return (frozenset((name, fingerprint(item, depth + 1, path, budget)) for name, item in state.items()),)


def own_state(value: Any, kind: Any) -> dict[str, Any]:
    state = {}
    try:
        own = object.__getattribute__(value, "__dict__")
    except AttributeError:
        own = {}

    if type(own) is dict:
        state.update(own)

    for name in slots(kind):
        try:
            state[name] = object.__getattribute__(value, name)
        except AttributeError:
            continue

    hidden = filled_on_use(kind, state) | SERVICE

    return {name: item for name, item in state.items() if name not in hidden}


@functools.cache
def data_class(kind: Any) -> bool:
    """Containers and classes of the project, built only on such classes and the standard library's"""
    for cls in kind.__mro__:
        module = module_of(cls)
        if module == "builtins":
            if cls in BUILTIN:
                continue
            return False
        if module.split(".")[0] in STANDARD or (module.split(".")[0], cls.__name__) in DATA:
            continue
        if not project_module(module):
            return False

    return True


def module_of(cls: type) -> str:
    """The module of a class, whose __module__ may be a property of its own"""
    module = vars(cls).get("__module__")
    if module is None:
        try:
            module = type.__dict__["__module__"].__get__(cls)
        except Exception:
            module = None

    return module if isinstance(module, str) else ""


def used(kind: Any) -> bool:
    connection = getattr(sys.modules.get("http.client"), "HTTPConnection", None)

    return issubclass(kind, USED) or (connection is not None and issubclass(kind, connection))


@functools.cache
def standard(kind: Any) -> bool:
    name = module_of(kind).split(".")[0]

    # A lock counts by identity, so an object of the standard library that holds one, as a queue or
    # an event, is still dumped
    return name != "_thread" and name.lstrip("_") in sys.stdlib_module_names


def project_module(name: str) -> bool:
    return ownership.own(sys.modules.get(name))


@functools.cache
def slots(kind: Any) -> tuple[str, ...]:
    names = []
    for cls in kind.__mro__:
        declared = vars(cls).get("__slots__", ())
        for name in (declared,) if isinstance(declared, str) else declared:
            if name not in ("__dict__", "__weakref__"):
                owner = cls.__name__.lstrip("_")
                if name.startswith("__") and not name.endswith("__") and owner:
                    name = f"_{owner}{name}"

                names.append(name)

    return tuple(names)


@functools.cache
def cached_properties(kind: Any) -> frozenset[str]:
    return frozenset(
        name
        for cls in kind.__mro__
        for name, attribute in vars(cls).items()
        if isinstance(attribute, functools.cached_property) or type(attribute).__name__ == "cached_property"
    )


def filled_on_use(kind: type, state: dict[str, Any]) -> frozenset[str]:
    """Cached properties, and a function of the class's own code it puts on the object, as a cached
    logger does. A function a test puts there is a change"""
    return cached_properties(kind) | {name for name, item in state.items() if own_function(kind, name, item)}


def own_function(kind: type, name: str, item: Any) -> bool:
    if not callable(item) or not callable(getattr(kind, name, None)):
        return False

    qualname = getattr(item, "__qualname__", None)
    if not isinstance(qualname, str):
        return False

    return any(
        qualname.startswith(f"{cls.__qualname__}.") and getattr(item, "__module__", None) == cls.__module__
        for cls in kind.__mro__
    )


# Unsupported values are observed but never restored as if they were missing
UNSUPPORTED = object()
REVERSIBLE = {type(None), bool, int, float, str, bytes}


class Scalars:
    """Only direct scalar attributes, without copying or restoring arbitrary object graphs."""

    def __init__(self) -> None:
        self.modules = Modules()
        # Keep the original weak reference even after collection: a replacement is never the original
        self.originals: dict[str, ReferenceType[ModuleType]] = {}

    def snapshot(self) -> dict[str, Any]:
        state = {}
        for (module, attribute), value in self.modules.values().items():
            value_module = sys.modules.get(module)
            if not isinstance(value_module, ModuleType):
                continue
            self.originals.setdefault(module, ref(value_module))
            state[f"{module}.{attribute}"] = value if type(value) in REVERSIBLE else UNSUPPORTED

        return state

    @staticmethod
    def label(name: str) -> str:
        return name

    @staticmethod
    def changed(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
        modules = {name.rpartition(".")[0] for name in before}
        changes = []
        for name in sorted(before.keys() | after.keys()):
            if name.rpartition(".")[0] not in modules:
                continue
            if before.get(name) is UNSUPPORTED or after.get(name) is UNSUPPORTED:
                continue
            if (name in before) != (name in after) or fingerprint(before.get(name), 0) != fingerprint(
                after.get(name),
                0,
            ):
                changes.append(name)

        return changes

    def restore(self, name: str, value: Any, *, exists: bool = True) -> None:
        module_name, _, attribute = name.rpartition(".")
        original = self.originals.get(module_name)
        module = original() if original is not None else None
        if module is None or sys.modules.get(module_name) is not module:
            raise ValueError("the observed module was replaced")

        if exists:
            vars(module)[attribute] = value
        else:
            vars(module).pop(attribute, None)
