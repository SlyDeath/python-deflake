import collections
import concurrent.futures
import datetime
import decimal
import functools
import importlib
import ipaddress
import itertools
import logging
import random
import re
import sqlite3
import sys
import time
import types
import warnings
from pathlib import Path
from typing import Any

import pydantic
import pytest

from deflake.leaks import modules
from deflake.leaks.modules import fingerprint


def test_a_key_put_back_is_the_same_dict() -> None:
    value = {"a": 1, "b": 2}
    before = fingerprint(value, 0)
    del value["a"]
    value["a"] = 1
    assert fingerprint(value, 0) == before


def test_a_changed_dict_is_changed() -> None:
    value = {"a": 1}
    before = fingerprint(value, 0)
    value["a"] = 2
    assert fingerprint(value, 0) != before


def test_a_random_generator_used_is_the_same() -> None:
    generator = random.Random(1)
    before = fingerprint(generator, 0)
    generator.random()
    assert fingerprint(generator, 0) == before


def test_a_query_set_is_never_evaluated() -> None:
    class QuerySet:
        __module__ = "django.db.models.query"

        def __getstate__(self) -> Any:
            raise AssertionError("evaluated")

    query = QuerySet()
    assert fingerprint(query, 0) == fingerprint(query, 0)


def test_url_patterns_filled_in_on_use_are_the_same() -> None:
    class URLPattern:
        __module__ = "django.urls.resolvers"

        def __init__(self) -> None:
            self.cache: dict[str, str] = {}

    patterns = [URLPattern()]
    before = fingerprint(patterns, 0)
    patterns[0].cache["lookup"] = "view"
    assert fingerprint(patterns, 0) == before
    patterns.append(URLPattern())
    assert fingerprint(patterns, 0) != before


class Service:
    def __init__(self) -> None:
        self.sender: Any = None


def test_an_object_changed_in_place_so_pickle_cannot_dump_it_is_changed() -> None:
    service = Service()
    before = fingerprint(service, 0)
    service.sender = lambda: None
    assert fingerprint(service, 0) != before


class Client:
    def __init__(self) -> None:
        self.token = ""


def test_a_class_of_the_project_named_as_a_library_one_is_compared_by_value() -> None:
    client = Client()
    before = fingerprint(client, 0)
    client.token = "x"
    assert fingerprint(client, 0) != before


class Storage:
    def __init__(self) -> None:
        self.location = "photos"

    @functools.cached_property
    def path(self) -> str:
        return "/srv/" + self.location


def test_a_cached_property_filled_in_on_use_is_not_a_change() -> None:
    storage = Storage()
    before = fingerprint(storage, 0)
    assert storage.path
    assert fingerprint(storage, 0) == before
    storage.location = "other"
    assert fingerprint(storage, 0) != before


def test_a_lazy_object_is_never_computed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "lazy_globals.py").write_text(
        "from django.utils.functional import SimpleLazyObject\n\n\n"
        "def fail():\n    raise RuntimeError('computed')\n\n\n"
        "FIRST = SimpleLazyObject(fail)\nSTATE = []\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(modules, "ROOT", tmp_path)
    lazy_globals = importlib.import_module("lazy_globals")

    detector = modules.Modules()
    before = detector.snapshot()
    lazy_globals.STATE.append(1)
    assert detector.diff(before, detector.snapshot()) == ["lazy_globals.STATE was changed"]
    assert before[("lazy_globals", "FIRST")][0] == "id"


def test_a_counter_moved_on_is_changed() -> None:
    counter = itertools.count()
    before = fingerprint(counter, 0)
    next(counter)
    assert fingerprint(counter, 0) != before


# Any, as a class built at run time stands in for Django's Client and takes any attribute
DjangoClient: Any = type("Client", (), {"__module__": "django.test.client"})


class Helper:
    def __init__(self) -> None:
        self.client = DjangoClient()
        self.token = ""


def test_an_object_holding_a_test_client_is_compared_but_for_the_client() -> None:
    helper = Helper()
    before = fingerprint(helper, 0)
    helper.client.session = lambda: None
    assert fingerprint(helper, 0) == before
    helper.token = "x"
    assert fingerprint(helper, 0) != before


def test_a_shared_freezer_used_as_a_decorator_is_the_same() -> None:
    # Any, as a class built at run time stands in for freezegun and takes any attribute
    freezer: Any = type("_freeze_time", (), {"__module__": "freezegun.api"})()
    before = fingerprint(freezer, 0)
    freezer.reals = {"time": 1}
    assert fingerprint(freezer, 0) == before


def test_a_proxy_that_fails_on_a_look_inside_leaves_the_others_watched(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # As werkzeug's LocalProxy for the request outside of a request
    (tmp_path / "proxy_globals.py").write_text(
        "class LocalProxy:\n    __slots__ = ()\n\n"
        "    def __getattr__(self, name):\n        raise RuntimeError('outside of a request')\n\n\n"
        "request = LocalProxy()\nSTATE = []\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(modules, "ROOT", tmp_path)
    proxy_globals = importlib.import_module("proxy_globals")

    detector = modules.Modules()
    before = detector.snapshot()
    proxy_globals.STATE.append(1)
    assert detector.diff(before, detector.snapshot()) == ["proxy_globals.STATE was changed"]


def test_a_lazy_object_proxy_is_never_computed(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []

    class Proxy:
        __module__ = property(lambda self: "computed")  # type: ignore[assignment]

        def __reduce__(self) -> Any:
            calls.append(1)
            raise RuntimeError("computed")

    monkeypatch.setitem(sys.modules, "lazy_object_proxy", types.SimpleNamespace(Proxy=Proxy))
    proxy = Proxy()
    assert fingerprint(proxy, 0) == fingerprint(proxy, 0)
    assert calls == []


class Logger:
    def bind(self) -> "Logger":
        # As structlog's lazy proxy caches the logger it made on first use
        self.bind = lambda: self  # type: ignore[method-assign]

        return self


def test_a_method_an_object_puts_on_itself_on_first_use_is_not_a_change() -> None:
    logger = Logger()
    before = fingerprint(logger, 0)
    logger.bind()
    assert fingerprint(logger, 0) == before


def test_a_c_type_tells_its_module_by_its_name() -> None:
    flags = type(sys.flags)
    assert "__module__" not in vars(flags)
    assert modules.module_of(flags) == "sys"


class StateLogger(Logger):
    def __getstate__(self) -> dict[str, Any]:
        return self.__dict__


class Registry(dict[str, int]):
    pass


class Items(list[int]):
    pass


class Slotted:
    __slots__ = ("value",)


class Child(Slotted):
    pass


class _PrivateSlotted:
    __slots__ = ("__value",)

    def set_value(self, value: int) -> None:
        self.__value = value


class PrivateChild(_PrivateSlotted):
    __slots__ = ("__value",)

    def set_child_value(self, value: int) -> None:
        self.__value = value


@pytest.mark.parametrize("dumped", [False, True], ids=["parts", "dump"])
@pytest.mark.parametrize("inherited", [False, True], ids=["own", "inherited"])
def test_a_private_slot_change_is_detected(dumped: bool, inherited: bool) -> None:
    value = PrivateChild()
    value.set_value(1)
    value.set_child_value(2)
    before = modules.dump(value) if dumped else fingerprint(value, 0)
    if inherited:
        value.set_value(3)
    else:
        value.set_child_value(3)

    after = modules.dump(value) if dumped else fingerprint(value, 0)
    assert after != before


class Point(collections.namedtuple("Point", "x y")):
    pass


def test_a_logger_with_a_state_of_its_own_caching_on_first_use_is_the_same() -> None:
    logger = StateLogger()
    before = fingerprint(logger, 0)
    logger.bind()
    assert fingerprint(logger, 0) == before


def test_a_method_a_test_puts_on_an_object_is_a_change() -> None:
    logger = Logger()
    before = fingerprint(logger, 0)
    logger.bind = lambda: logger  # type: ignore[method-assign]
    assert fingerprint(logger, 0) != before


@pytest.mark.parametrize(
    ("make", "change"),
    [
        (Registry, lambda value: value.update(a=1)),
        (Items, lambda value: value.append(1)),
        (Child, lambda value: setattr(value, "value", 2)),
    ],
    ids=["dict", "list", "slots"],
)
def test_a_change_outside_of_the_attributes_is_a_change(make: Any, change: Any) -> None:
    value = make()
    before = fingerprint(value, 0)
    change(value)
    assert fingerprint(value, 0) != before


def test_another_named_tuple_is_a_change() -> None:
    assert fingerprint(Point(1, 1), 0) != fingerprint(Point(1, 2), 0)


class Session:
    __module__ = "some_library.sessions"

    def __init__(self) -> None:
        self.adapters: dict[str, Any] = {}


def test_an_object_of_a_library_counts_by_identity() -> None:
    session = Session()
    before = fingerprint(session, 0)
    session.adapters["https"] = object()
    assert fingerprint(session, 0) == before
    assert fingerprint(Session(), 0) != before


def test_an_object_of_the_project_built_on_a_library_class_counts_by_identity() -> None:
    class Cached(Session):
        pass

    cached = Cached()
    before = fingerprint(cached, 0)
    cached.adapters["https"] = object()
    assert fingerprint(cached, 0) == before


def test_a_list_holding_itself_and_nan_is_compared() -> None:
    value: list[object] = [float("nan")]
    value.append(value)
    assert fingerprint(value, 0) == fingerprint(value, 0)
    value.append(1)
    assert fingerprint(value, 0) != fingerprint([float("nan")], 0)


def test_a_large_table_is_compared_quickly() -> None:
    table = [{f"field{n}": f"value{i}" for n in range(8)} for i in range(5000)]
    started = time.monotonic()
    before = fingerprint(table, 0)
    assert time.monotonic() - started < 0.5
    assert fingerprint(table, 0) == before
    table[4999]["field0"] = "changed"
    assert fingerprint(table, 0) != before


def test_python_of_its_own_in_the_project_folder_is_not_the_project(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(modules, "ROOT", Path(re.__file__).resolve().parents[2])
    assert not modules.project_file(re.__file__)


def test_objects_of_the_standard_library_changed_on_use_are_the_same() -> None:
    context = decimal.Context(prec=2)
    storage = Storage()
    namespace = types.SimpleNamespace(storage=storage)
    send = functools.partial(print, storage)
    before = [fingerprint(value, 0) for value in (context, namespace, send)]
    decimal.Decimal("1.555").quantize(decimal.Decimal("0.1"), context=context)
    assert storage.path
    assert [fingerprint(value, 0) for value in (context, namespace, send)] == before
    storage.location = "videos"
    assert fingerprint(namespace, 0) != before[1]
    assert fingerprint(send, 0) != before[2]


def test_a_bound_method_replaced_in_a_container_is_a_change() -> None:
    first, second = Storage(), Storage()
    hub = {"sender": first.__init__}
    before = fingerprint(hub, 0)
    hub["sender"] = second.__init__
    assert fingerprint(hub, 0) != before


def test_a_scalar_without_a_hash_leaves_the_rest_compared() -> None:
    class Zone(datetime.tzinfo):
        __hash__ = None  # type: ignore[assignment]

    value = {"tz": Zone(), "n": 1}
    before = fingerprint(value, 0)
    value["n"] = 2
    assert fingerprint(value, 0) != before


def test_a_counter_is_compared_on_every_version_of_python() -> None:
    counter = itertools.count()
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        before = fingerprint(counter, 0)

    next(counter)
    assert fingerprint(counter, 0) != before


def test_an_object_of_a_class_that_cannot_be_cached_counts_by_identity() -> None:
    class Meta(type):
        def __eq__(cls, other: object) -> bool:
            return cls is other

    class Odd(metaclass=Meta):
        pass

    assert fingerprint(Odd(), 0)[0] == "id"


def test_many_objects_of_the_project_are_compared_by_the_same_rules() -> None:
    storages = [Storage() for _ in range(700)]
    tags = {f"user{i}": {"a", "b", "c", "d"} for i in range(500)}
    before = (fingerprint(storages, 0), fingerprint(tags, 0))
    assert all(storage.path for storage in storages)
    tags["user1"].add("tmp")
    tags["user1"].discard("tmp")
    assert (fingerprint(storages, 0), fingerprint(tags, 0)) == before


def test_a_dump_follows_the_rules_of_the_comparison() -> None:
    storage = Storage()
    value = [storage, Session(), lambda: None, {"n": 1}]
    before = modules.dump(value)
    assert storage.path
    assert modules.dump(value) == before
    value[3]["n"] = 2  # type: ignore[index]
    assert modules.dump(value) != before


def test_a_pydantic_field_set_back_is_the_same() -> None:
    class Config(pydantic.BaseModel):
        debug: bool = False

    config = Config()
    before = fingerprint(config, 0)
    config.debug = True
    assert fingerprint(config, 0) != before
    config.debug = False
    assert fingerprint(config, 0) == before


def test_a_table_of_mixed_scalars_is_compared_quickly_and_by_type() -> None:
    table = [{"id": i, "name": f"c{i}", "active": True, "price": 1.5} for i in range(10_000)]
    started = time.monotonic()
    before = fingerprint(table, 0)
    assert time.monotonic() - started < 0.5
    table[0]["active"] = 1
    assert fingerprint(table, 0) != before
    table[0]["active"] = True
    del table[0]["id"]
    table[0]["id"] = 0
    assert fingerprint(table, 0) == before
    assert fingerprint([float("nan")], 0) == fingerprint([float("nan")], 0)
    assert fingerprint([1], 0) != fingerprint([1.0], 0)


class Peer:
    def __init__(self) -> None:
        self.peer: Any = None
        self.name = "a"


def test_a_dump_goes_through_cycles_and_objects_it_cannot_dump() -> None:
    first, second = Peer(), Peer()
    first.peer, second.peer = second, first
    connection = sqlite3.connect(":memory:")
    value = [first, connection]
    before = modules.dump(value)
    assert before[0] != "id"
    first.name = "b"
    assert modules.dump(value) != before
    connection.close()


def test_a_thread_pool_used_is_the_same() -> None:
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=2)
    try:
        before = fingerprint(pool, 0)
        for _ in range(3):
            list(pool.map(time.sleep, [0.01] * 4))
            assert fingerprint(pool, 0) == before
            assert modules.dump([pool]) == modules.dump([pool])
    finally:
        pool.shutdown()


def test_a_dump_goes_through_cycles_of_containers_of_the_project() -> None:
    registry = Registry()
    node = Peer()
    node.peer = registry
    registry["a"] = node  # type: ignore[assignment]
    before = modules.dump([registry])
    assert before[0] != "id"
    node.name = "b"
    assert modules.dump([registry]) != before


def test_a_log_handler_opening_its_file_on_first_use_is_the_same(tmp_path: Path) -> None:
    handler = logging.FileHandler(tmp_path / "log", delay=True)
    before = fingerprint(handler, 0)
    handler.emit(logging.makeLogRecord({"msg": "hello"}))
    assert fingerprint(handler, 0) == before
    handler.close()


def test_addresses_are_dumped_as_scalars(monkeypatch: pytest.MonkeyPatch) -> None:
    # An address goes into a whole dump as it reduces, not dumped strictly on its own
    one_by_one = []
    original = modules.dump

    def dump(value: Any, *, strict: bool = False) -> Any:
        if strict:
            one_by_one.append(value)

        return original(value, strict=strict)

    monkeypatch.setattr(modules, "dump", dump)

    networks = [ipaddress.ip_network(f"10.0.{i}.0/24") for i in range(100)]
    before = modules.dump(networks)
    assert one_by_one == []
    networks[0] = ipaddress.ip_network("192.168.0.0/24")
    assert modules.dump(networks) != before


def test_an_object_reduced_to_a_lone_callable_leaves_a_large_global_compared() -> None:
    # A class of the standard library as far as the detector can tell
    lone = type("Lone", (), {"__module__": "json", "__reduce_ex__": lambda self, protocol: (type(self),)})
    numbers: list[object] = [lone(), *range(60_000)]
    before = fingerprint(numbers, 0)
    numbers[1] = -1
    assert fingerprint(numbers, 0) != before


def test_an_unavailable_module_is_not_owned():
    assert not modules.Modules().own(None)


def test_scalar_probe_does_not_treat_a_function_as_a_deleted_value(tmp_path, monkeypatch):
    module = types.ModuleType("scalar_case")
    module.__file__ = str(tmp_path / "scalar_case.py")
    vars(module)["value"] = 1
    monkeypatch.setattr(modules, "ROOT", tmp_path)
    monkeypatch.setitem(sys.modules, module.__name__, module)
    detector = modules.Scalars()
    before = detector.snapshot()
    vars(module)["value"] = lambda: 1
    assert detector.changed(before, detector.snapshot()) == []


def test_unchanged_nan_is_not_a_scalar_intervention_candidate():
    assert modules.Scalars.changed({"sample.VALUE": float("nan")}, {"sample.VALUE": float("nan")}) == []


def test_module_ownership_does_not_keep_an_unloaded_module_alive():
    import gc
    import weakref

    module = types.ModuleType("unloaded_case")
    module.__file__ = str(modules.ROOT / "unloaded_case.py")
    detector = modules.Modules()
    assert detector.own(module)
    reference = weakref.ref(module)
    del module
    gc.collect()
    assert reference() is None


def test_scalar_probe_rejects_replacement_after_original_module_is_collected(tmp_path, monkeypatch):
    import gc
    import weakref

    original = types.ModuleType("replacement_case")
    original.__file__ = str(tmp_path / "replacement_case.py")
    vars(original)["FLAG"] = "original"
    monkeypatch.setattr(modules, "ROOT", tmp_path)
    monkeypatch.setitem(sys.modules, original.__name__, original)
    detector = modules.Scalars()
    before = detector.snapshot()
    reference = weakref.ref(original)
    del original
    replacement = types.ModuleType("replacement_case")
    replacement.__file__ = str(tmp_path / "replacement_case.py")
    vars(replacement)["FLAG"] = "leaked"
    sys.modules[replacement.__name__] = replacement
    gc.collect()
    assert reference() is None
    detector.snapshot()
    with pytest.raises(ValueError, match="replaced"):
        detector.restore("replacement_case.FLAG", before["replacement_case.FLAG"])


@pytest.mark.parametrize("large", [False, True])
@pytest.mark.parametrize("metadata", [None, "/outside/site-packages/state.py"])
def test_new_project_objects_stay_observable_after_module_metadata_changes(
    tmp_path,
    monkeypatch,
    large,
    metadata,
):
    module = types.ModuleType("metadata_objects")
    module.__file__ = str(tmp_path / "metadata_objects.py")
    exec("class Item:\n    def __init__(self):\n        self.value = 0\nOBJ = None\n", vars(module))
    monkeypatch.setattr(modules, "ROOT", tmp_path)
    monkeypatch.setitem(sys.modules, module.__name__, module)
    if large:
        monkeypatch.setattr(modules, "NODES", 1)

    detector = modules.Modules()
    detector.snapshot()
    module.__file__ = metadata
    vars(module)["OBJ"] = vars(module)["Item"]()
    before = detector.snapshot()
    if large:
        assert before[(module.__name__, "OBJ")][0] == "dump"

    vars(module)["OBJ"].value = 1
    assert "metadata_objects.OBJ was changed" in detector.diff(before, detector.snapshot())
