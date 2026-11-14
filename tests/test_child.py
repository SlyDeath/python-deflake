import random
import sys
import threading
import types

import pytest

from deflake import child
from deflake.leaks.threads import Threads


def test_reseed_seeds_the_libraries_the_tests_use(monkeypatch: pytest.MonkeyPatch) -> None:
    seeds: dict[str, int] = {}

    class Faker:
        @staticmethod
        def seed(value: int) -> None:
            seeds["faker"] = value

    monkeypatch.setitem(sys.modules, "faker", types.SimpleNamespace(Faker=Faker))
    monkeypatch.setitem(
        sys.modules,
        "factory.random",
        types.SimpleNamespace(reseed_random=lambda value: seeds.__setitem__("factory", value)),
    )
    monkeypatch.setitem(
        sys.modules,
        "numpy.random",
        types.SimpleNamespace(seed=lambda value: seeds.__setitem__("numpy", value)),
    )
    child.reseed(2**33 + 5)
    assert seeds == {"faker": 2**33 + 5, "factory": 2**33 + 5, "numpy": 5}
    first = random.random()
    child.reseed(2**33 + 5)
    assert random.random() == first


def test_a_library_that_fails_to_seed_does_not_break_the_test(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(value: int) -> None:
        raise TypeError("changed")

    monkeypatch.setitem(sys.modules, "numpy.random", types.SimpleNamespace(seed=broken))
    child.reseed(1)


def test_a_seed_gives_one_order_however_the_tests_were_collected() -> None:
    tests = [f"{module}::test_{name}" for module in "abc" for name in range(5)]
    request = child.Request(output=None, seed=7)
    orders = set()
    for _ in range(5):
        collected = random.sample(tests, len(tests))
        orders.add(tuple(child.arrange(collected, request, str, lambda test: [test.split("::")[0]])))

    assert len(orders) == 1


def test_a_thread_without_a_name_is_not_told_by_its_number() -> None:
    detector = Threads()
    before = detector.snapshot()
    stop = threading.Event()
    thread = threading.Thread(target=stop.wait)
    thread.start()
    try:
        assert detector.diff(before, detector.snapshot()) == ["thread 'Thread-N (wait)' is still running"]
    finally:
        stop.set()
        thread.join()


def test_environment_probe_keeps_values_private_and_reapplies(tmp_path, monkeypatch):
    from deflake import child, events

    monkeypatch.setenv("CAUSE_FLAG", "original-private-value")
    output = tmp_path / "events.jsonl"
    recorder = child.Recorder(
        str(output),
        1,
        {"culprit": "a", "mode": "reapply", "key": "environment:CAUSE_FLAG"},
    )
    recorder.started("a", "a")
    monkeypatch.setenv("CAUSE_FLAG", "leaked-private-value")
    recorder.finished("a", "a", passed=True, leaked=["os.environ['CAUSE_FLAG'] was changed"])
    import json
    import os

    assert os.environ["CAUSE_FLAG"] == "leaked-private-value"
    written = output.read_text()
    assert "private-value" not in written
    row = json.loads(written.splitlines()[-1])
    assert row["intervened"] is True
    assert row["environment_changes"] == ["environment:CAUSE_FLAG"]
    assert events.PROBE_KEY in events.KEYS


def test_process_tracking_preserves_the_popen_class_and_forwards_arguments(tmp_path, monkeypatch):
    calls = []
    callbacks = []

    class Popen:
        def __init__(self, *args, **kwargs):
            calls.append((args, kwargs))
            self.pid = 123

    monkeypatch.setattr(child.subprocess, "Popen", Popen)
    monkeypatch.setattr(
        child.os,
        "register_at_fork",
        lambda **kwargs: callbacks.append(kwargs["after_in_child"]),
    )
    monkeypatch.setattr(child.events, "process_identity", lambda pid: str(pid))
    monkeypatch.setattr(child.os, "posix_spawn", lambda *args, **kwargs: 456)
    monkeypatch.setattr(child.os, "posix_spawnp", lambda *args, **kwargs: 789)
    # Restore these public API functions after watch_processes wraps them
    original_spawn = child.os.posix_spawn
    original_spawnp = child.os.posix_spawnp
    output = tmp_path / "results.jsonl"
    child.watch_processes(str(output))
    try:
        process = child.subprocess.Popen(["service"], env={}, start_new_session=True)
        assert type(process) is Popen
        assert calls == [((["service"],), {"env": {}, "start_new_session": True})]
        assert child.os.posix_spawn("service", ["service"], {}) == 456
        assert child.os.posix_spawnp("service", ["service"], {}) == 789
        callbacks[0]()
    finally:
        child.os.posix_spawn = original_spawn
        child.os.posix_spawnp = original_spawnp

    import json

    rows = [json.loads(line) for line in child.events.process_file(output).read_text().splitlines()]
    assert [row["pid"] for row in rows] == [123, 456, 789, child.os.getpid()]


@pytest.mark.parametrize("recording", [True, False])
@pytest.mark.parametrize("error", [RuntimeError("late error"), SystemExit(1), KeyboardInterrupt()])
def test_recording_errors_preserves_the_original_exception(tmp_path, recording, error):
    import json

    output = tmp_path / "results.jsonl"
    recorder = child.Recorder(str(output), 1) if recording else None
    with (
        pytest.raises(type(error)) as raised,
        child.recording_errors(recorder, "runner finalization"),
    ):
        raise error

    assert raised.value is error
    if recording:
        rows = [json.loads(line) for line in output.read_text().splitlines()]
        assert rows[-1]["outside"] == f"runner finalization: {type(error).__name__}: {error}"
    else:
        assert not output.exists()


def test_a_change_the_test_is_not_reported_with_is_not_probed(tmp_path, monkeypatch):
    import json

    from deflake import child

    output = tmp_path / "events.jsonl"
    recorder = child.Recorder(
        str(output),
        1,
        {"culprit": "a", "mode": "repair", "key": "environment:FIXTURE_FLAG"},
    )
    recorder.started("a", "a")
    # As a wider fixture set up with the test, whose change the report shows as the fixture's
    monkeypatch.setenv("FIXTURE_FLAG", "1")
    recorder.finished("a", "a", passed=True, leaked=[])
    row = json.loads(output.read_text().splitlines()[-1])
    assert row["environment_changes"] == []
    assert row["intervened"] is False


def test_only_a_kind_whose_snapshot_failed_is_left_out(monkeypatch: pytest.MonkeyPatch) -> None:
    from deflake.leaks import states

    class Broken:
        def snapshot(self) -> dict[str, str]:
            raise RuntimeError("unavailable")

    monkeypatch.setitem(states.kinds, "broken", Broken)
    probed = states.Probed()
    monkeypatch.setenv("ONLY_FLAG", "1")
    before = probed.snapshot()
    assert before[states.FAILED] == {"broken"}
    # As after os.environ.clear()
    after = {key: value for key, value in before.items() if not key.startswith("environment:")}
    assert "environment:ONLY_FLAG" in probed.changed(before, after)
    assert probed.changed(before, {**after, states.FAILED: {"environment"}}) == []


def test_a_patch_is_stopped_and_its_own_mock_set_again() -> None:
    import json
    from unittest import mock

    from deflake.leaks import states

    original = json.dumps
    patched = states.Patched()
    before = patched.read()
    mock.patch("json.dumps", return_value="patched").start()
    try:
        after = patched.read()
        assert patched.changed(before, after) == ["mock.patch of json.dumps"]
        patched.restore("mock.patch of json.dumps", None, exists=False)
        assert json.dumps(1) == "1"
        patched.restore("mock.patch of json.dumps", after["mock.patch of json.dumps"])
        # The culprit's mock, with what it set on it
        assert json.dumps(1) == "patched"
    finally:
        mock.patch.stopall()
        json.dumps = original


def test_a_patch_set_again_keeps_its_place_among_the_others() -> None:
    from unittest import mock

    from deflake.leaks import states

    settings = {"mode": "real"}
    mock.patch.dict(settings, {"first": "A"}).start()
    mock.patch.dict(settings, {"second": "B"}).start()
    try:
        patched = states.Patched()
        after = patched.read()
        patched.restore("mock.patch of a dict", None, exists=False)
        patched.restore("mock.patch of a dict", after["mock.patch of a dict"])
        assert [stored[0][0] for stored in patched.read().values()] == [
            stored[0][0] for stored in after.values()
        ]
        mock.patch.stopall()
        assert settings == {"mode": "real"}
    finally:
        mock.patch.stopall()


@pytest.mark.parametrize("kind", ["patch", "autospec", "new_callable", "multiple", "dict", "cleared dict"])
@pytest.mark.parametrize("ending", ["stopall", "stop"])
def test_a_patch_set_again_is_undone_as_in_the_culprits_run(kind: str, ending: str) -> None:
    import json
    from unittest import mock

    from deflake.leaks import states

    original = (json.dumps, json.loads)
    settings = {"mode": "real"}
    patcher = {
        "patch": lambda: mock.patch("json.dumps", return_value="patched"),
        "autospec": lambda: mock.patch("json.dumps", autospec=True, return_value="patched"),
        "new_callable": lambda: mock.patch("json.dumps", new_callable=lambda: lambda *args: "patched"),
        "multiple": lambda: mock.patch.multiple("json", dumps=lambda *args: "patched", loads=lambda *args: 0),
        "dict": lambda: mock.patch.dict(settings, {"mode": "patched", "gone": "deleted"}),
        "cleared dict": lambda: mock.patch.dict(settings, {"mode": "patched", "gone": "deleted"}, clear=True),
    }[kind]()
    patcher.start()
    try:
        # The culprit also writes into the dict it patched, which stopping the patch takes back
        if kind.endswith("dict"):
            settings["written"] = "by the culprit"
            del settings["gone"]
        expected = dict(settings)
        patched = states.Patched()
        after = patched.read()
        (name,) = after
        patched.restore(name, None, exists=False)
        patched.restore(name, after[name])
        assert settings == expected
        assert json.dumps(1) == ("1" if kind.endswith("dict") else "patched")
        if ending == "stopall":
            mock.patch.stopall()
        else:
            patcher.stop()
        assert (json.dumps, json.loads) == original
        assert settings == {"mode": "real"}
    finally:
        mock.patch.stopall()
        json.dumps, json.loads = original


def test_a_patch_under_another_of_its_target_is_not_stopped() -> None:
    from unittest import mock

    from deflake.leaks import states

    patched = states.Patched()
    mock.patch("json.dumps", return_value="lower").start()
    mock.patch("json.dumps", return_value="upper").start()
    try:
        assert patched.label("mock.patch of json.dumps #2") == "mock.patch of json.dumps"
        # Stopping the lower one would put back the original under the upper one
        with pytest.raises(ValueError):
            patched.restore("mock.patch of json.dumps", None, exists=False)
        patched.restore("mock.patch of json.dumps #2", None, exists=False)
    finally:
        mock.patch.stopall()


def test_a_patched_dict_gets_back_only_its_own_keys() -> None:
    from unittest import mock

    from deflake.leaks import states

    settings = {"mode": "real"}
    patched = states.Patched()
    mock.patch.dict(settings, {"mode": "patched"}).start()
    try:
        after = patched.read()
        settings["other"] = "kept"
        patched.restore("mock.patch of a dict", None, exists=False)
        assert settings == {"mode": "real", "other": "kept"}
        patched.restore("mock.patch of a dict", after["mock.patch of a dict"])
        assert settings == {"mode": "patched", "other": "kept"}
    finally:
        mock.patch.stopall()


def test_every_attribute_of_a_patch_is_set_again_as_it_was_stored() -> None:
    from unittest import mock

    from deflake.leaks import states

    class Target:
        def first(self) -> str:
            return "real"

        second = "real"

    patched = states.Patched()
    mock.patch.multiple(Target, first=staticmethod(lambda: "patched"), second="patched").start()
    try:
        after = patched.read()
        (name,) = after
        patched.restore(name, None, exists=False)
        assert (Target().first(), Target.second) == ("real", "real")
        patched.restore(name, after[name])
        # Still a staticmethod, so an instance does not pass itself
        assert (Target().first(), Target.second) == ("patched", "patched")
    finally:
        mock.patch.stopall()


def test_a_patch_of_another_attribute_over_a_patch_keeps_it_started() -> None:
    from unittest import mock

    from deflake.leaks import states

    class Target:
        first = "real"
        second = "real"

    patched = states.Patched()
    mock.patch.multiple(Target, first="patched", second="patched").start()
    mock.patch.object(Target, "second", "later").start()
    try:
        # Stopping the first would put the second back under the later patch
        with pytest.raises(ValueError):
            patched.restore("mock.patch of Target.first", None, exists=False)
    finally:
        mock.patch.stopall()


def test_a_patch_the_culprit_started_in_place_of_another_is_stopped() -> None:
    from unittest import mock

    from deflake.leaks import states

    class Target:
        value = "real"

    patched = states.Patched()
    fixture = mock.patch.object(Target, "value", "fixture")
    fixture.start()
    before = patched.read()
    # As a wider fixture torn down in the culprit's window, which starts a patch of its own
    fixture.stop()
    mock.patch.object(Target, "value", "culprit").start()
    try:
        name = "mock.patch of Target.value"
        after = patched.read()
        patched.restore(name, before[name])
        assert Target.value == "real"
        patched.restore(name, after[name])
        assert Target.value == "culprit"
    finally:
        mock.patch.stopall()
        Target.value = "real"


def test_frozen_time_is_let_go_and_frozen_again_with_the_culprit_clock() -> None:
    import datetime

    import freezegun

    from deflake.leaks import states

    frozen = states.Frozen()
    freezer = freezegun.freeze_time("2000-01-01")
    freezer.start()
    try:
        after = frozen.read()
        frozen.restore("freezegun", None, exists=False)
        assert datetime.date.today().year != 2000
        frozen.restore("freezegun", after["freezegun"])
        assert datetime.date.today().year == 2000
    finally:
        # Whatever failed, time goes on for the other tests
        frozen.restore("freezegun", None, exists=False)

    assert datetime.date.today().year != 2000


def test_time_frozen_before_the_culprit_stays_frozen() -> None:
    import datetime

    import freezegun

    from deflake.leaks import states

    frozen = states.Frozen()
    outer = freezegun.freeze_time("1990-01-01")
    outer.start()
    try:
        before = frozen.read()
        freezegun.freeze_time("2000-01-01").start()
        after = frozen.read()
        frozen.restore("freezegun", before["freezegun"])
        assert datetime.date.today().year == 1990
        frozen.restore("freezegun", after["freezegun"])
        assert datetime.date.today().year == 2000
        frozen.restore("freezegun", before["freezegun"])
    finally:
        frozen.restore("freezegun", None, exists=False)

    assert datetime.date.today().year not in {1990, 2000}


def test_a_patch_on_a_proxy_or_in_a_slot_is_set_again_as_it_reads() -> None:
    from unittest import mock

    from deflake.leaks import states

    class Slotted:
        __slots__ = ("value",)

        def __init__(self) -> None:
            self.value = "real"

    class Proxy:
        def __init__(self) -> None:
            object.__setattr__(self, "target", Slotted())

        def __getattr__(self, name: str) -> object:
            return getattr(self.target, name)

        def __setattr__(self, name: str, value: object) -> None:
            setattr(self.target, name, value)

        def __delattr__(self, name: str) -> None:
            delattr(self.target, name)

    slotted, proxy = Slotted(), Proxy()
    patched = states.Patched()
    mock.patch.object(slotted, "value", "patched").start()
    mock.patch.object(proxy, "value", "patched").start()
    try:
        after = patched.read()
        assert len(after) == 2
        for name in after:
            patched.restore(name, None, exists=False)
        assert (slotted.value, proxy.value) == ("real", "real")
        for name in after:
            patched.restore(name, after[name])
        assert (slotted.value, proxy.value) == ("patched", "patched")
    finally:
        mock.patch.stopall()


def test_a_patch_is_set_again_when_another_took_its_name() -> None:
    from unittest import mock

    from deflake.leaks import states

    first, second = {"key": "real"}, {"key": "real"}
    patched = states.Patched()
    mock.patch.dict(first, {"key": "patched"}).start()
    mock.patch.dict(second, {"key": "patched"}).start()
    try:
        after = patched.read()
        patched.restore("mock.patch of a dict", None, exists=False)
        assert (first, second) == ({"key": "real"}, {"key": "patched"})
        # The second patch is the first under that name now, and is left alone
        patched.restore("mock.patch of a dict", after["mock.patch of a dict"])
        assert (first, second) == ({"key": "patched"}, {"key": "patched"})
    finally:
        mock.patch.stopall()
