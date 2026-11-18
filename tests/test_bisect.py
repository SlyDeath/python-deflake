from typing import Any

import pytest

from deflake import execution
from deflake.bisect import Bisect, environment_causes, minimize
from deflake.models import same_failure


class Adapter:
    def reproduce(self, path: str, seed: int) -> str:
        return "cmd"

    def group(self, id: str) -> str | None:
        return None


def test_minimize_rechecks_candidates_after_removing_a_cleanup() -> None:
    def fails(tests: list[str]) -> bool:
        cleanup = True
        dirty = False
        for test in tests:
            if test == "disable":
                cleanup = False
            elif test == "pollute":
                dirty = True
            elif test == "clean" and cleanup:
                dirty = False

        return dirty

    assert minimize(["disable", "unrelated", "pollute", "clean"], fails) == ["pollute"]


def test_a_culprit_behind_a_test_that_hung_once_is_still_found(monkeypatch: pytest.MonkeyPatch) -> None:
    full = ["a", "hangs", "culprit", "victim"]
    orders = [["a", "hangs"], full]
    hung = {"left": 1}

    def order(adapter: Any, path: str, seed: int) -> list[str]:
        return orders.pop(0)

    def in_order(adapter: Any, path: str, ids: list[str], seed: int) -> list[dict[str, Any]]:
        if "hangs" in ids and hung["left"]:
            hung["left"] -= 1
            return [{"id": "hangs", "passed": False, "timed_out": True}]

        return [{"id": "victim", "passed": "culprit" not in ids}]

    monkeypatch.setattr(execution, "order", order)
    monkeypatch.setattr(execution, "in_order", in_order)
    culprits = Bisect(Adapter(), "test_x.py", 1)("victim")  # type: ignore[arg-type]
    assert culprits.ids == ["culprit"]


@pytest.mark.parametrize(("culprit", "reached"), [({}, False), ({"environment_changes": []}, True)])
def test_a_culprit_not_measured_tells_nothing_about_its_changes(
    monkeypatch: pytest.MonkeyPatch,
    culprit: dict[str, Any],
    reached: bool,
) -> None:
    def in_order(*arguments: Any, **options: Any) -> list[dict[str, Any]]:
        return [{"id": "culprit", **culprit}, {"id": "victim", "passed": False, "error": "boom"}]

    monkeypatch.setattr(execution, "in_order", in_order)
    assert environment_causes(Adapter(), "test_x.py", "victim", ["culprit"], 1, "boom") == ([], reached)  # type: ignore[arg-type]


def test_a_culprit_of_another_failure_of_the_victim_is_not_named(monkeypatch: pytest.MonkeyPatch) -> None:
    def order(adapter: Any, path: str, seed: int) -> list[str]:
        return ["sets_a", "sets_b", "victim"]

    def in_order(adapter: Any, path: str, ids: list[str], seed: int) -> list[dict[str, Any]]:
        # The victim checks a, then b: with a left set it fails at its first line
        if "sets_a" in ids:
            return [{"id": "victim", "passed": False, "error": "AssertionError: A leaked\n  at test_x.py:3"}]
        if "sets_b" in ids:
            return [{"id": "victim", "passed": False, "error": "AssertionError: B leaked\n  at test_x.py:4"}]

        return [{"id": "victim", "passed": True}]

    monkeypatch.setattr(execution, "order", order)
    monkeypatch.setattr(execution, "in_order", in_order)
    seen = "AssertionError: A leaked\n  at test_x.py:3"
    assert Bisect(Adapter(), "test_x.py", 1, seen)("victim").ids == ["sets_a"]  # type: ignore[arg-type]


def test_the_same_failure_with_other_values_is_still_the_victims() -> None:
    seen = "assert 'P' not in environ({'A': '1', 'P': '1'})\n  at test_x.py:3"
    other = "assert 'P' not in environ({'P': '1'})\n  at test_x.py:3"
    assert same_failure(seen, other, file="tests/test_x.py")
    assert not same_failure(seen, other)
    assert not same_failure(seen, "KeyError: 'P'\n  at test_x.py:3", file="test_x.py")
    assert not same_failure(seen, "assert 'P' not in environ({'P': '1'})\n  at test_x.py:9", file="test_x.py")


def test_errors_of_a_kind_at_a_line_of_a_library_are_not_one_failure() -> None:
    # Every KeyError of os.environ is raised at the same line of the os module
    seen = "KeyError: 'A'\n  at <frozen os>:714"
    assert not same_failure(seen, "KeyError: 'B'\n  at <frozen os>:714", file="test_x.py")


def test_a_test_kept_by_a_lucky_pass_of_the_victim_is_not_a_culprit(monkeypatch: pytest.MonkeyPatch) -> None:
    lucky = {"left": 1}

    def order(adapter: Any, path: str, seed: int) -> list[str]:
        return ["innocent", "culprit", "victim"]

    def in_order(adapter: Any, path: str, ids: list[str], seed: int) -> list[dict[str, Any]]:
        # The culprit breaks the victim only now and then: once it passes when the innocent is left out
        if ids == ["culprit", "victim"] and lucky["left"]:
            lucky["left"] -= 1
            return [{"id": "victim", "passed": True}]

        return [{"id": "victim", "passed": "culprit" not in ids}]

    monkeypatch.setattr(execution, "order", order)
    monkeypatch.setattr(execution, "in_order", in_order)
    culprits = Bisect(Adapter(), "test_x.py", 1)("victim")  # type: ignore[arg-type]
    assert culprits.ids == ["culprit"]


def test_each_test_that_breaks_the_victim_on_its_own_is_named(monkeypatch: pytest.MonkeyPatch) -> None:
    def order(adapter: Any, path: str, seed: int) -> list[str]:
        return ["first", "unrelated", "second", "victim"]

    def in_order(adapter: Any, path: str, ids: list[str], seed: int) -> list[dict[str, Any]]:
        return [{"id": "victim", "passed": not {"first", "second"} & set(ids)}]

    monkeypatch.setattr(execution, "order", order)
    monkeypatch.setattr(execution, "in_order", in_order)
    culprits = Bisect(Adapter(), "test_x.py", 1)("victim")  # type: ignore[arg-type]
    assert sorted([*culprits.ids, *(id for ids in culprits.others for id in ids)]) == ["first", "second"]
    assert len(culprits.others) == 1


def test_a_failure_that_comes_back_but_not_every_time_is_told_apart(monkeypatch: pytest.MonkeyPatch) -> None:
    runs = {"left": 2}

    def order(adapter: Any, path: str, seed: int) -> list[str]:
        return ["a", "b", "victim"]

    def in_order(adapter: Any, path: str, ids: list[str], seed: int) -> list[dict[str, Any]]:
        runs["left"] -= 1

        return [{"id": "victim", "passed": runs["left"] < 0 or len(ids) < 3}]

    monkeypatch.setattr(execution, "order", order)
    monkeypatch.setattr(execution, "in_order", in_order)
    culprits = Bisect(Adapter(), "test_x.py", 1)("victim")  # type: ignore[arg-type]
    assert (culprits.ids, culprits.reproduced, culprits.broken) == ([], True, False)


def test_runs_that_never_start_tell_nothing_of_the_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def order(adapter: Any, path: str, seed: int) -> list[str]:
        raise execution.NoResults("test_x.py", "", "exit code 1")

    monkeypatch.setattr(execution, "order", order)
    monkeypatch.setattr(execution, "retrying", lambda call: call())
    culprits = Bisect(Adapter(), "test_x.py", 1)("victim")  # type: ignore[arg-type]
    assert (culprits.ids, culprits.reproduced, culprits.broken) == ([], False, True)


def test_changes_of_a_culprit_are_tried_up_to_a_limit_loggers_last(monkeypatch: pytest.MonkeyPatch) -> None:
    loggers = [f"process:the disabled setting of logger 'app{number}'" for number in range(30)]
    changed = [*loggers, "process:the working directory", "django:settings.DEBUG", "environment:FLAG"]
    tried: list[str] = []

    def in_order(*arguments: Any, probe: dict[str, str], **options: Any) -> list[dict[str, Any]]:
        if probe["mode"] != "baseline":
            tried.append(probe["key"])

        culprit = {"id": "culprit", "environment_changes": changed, "intervened": True}

        return [culprit, {"id": "victim", "passed": False, "error": "boom"}]

    monkeypatch.setattr(execution, "in_order", in_order)
    causes, _ = environment_causes(Adapter(), "test_x.py", "victim", ["culprit"], 1, "boom")  # type: ignore[arg-type]
    assert len(causes) == 20
    assert (causes[0].detector, causes[0].key) == ("environment", "FLAG")
    assert tried[0] == "environment:FLAG"
    assert [cause.key for cause in causes[1:3]] == ["settings.DEBUG", "the working directory"]


def test_a_baseline_with_another_failure_tries_no_repair(monkeypatch: pytest.MonkeyPatch) -> None:
    def in_order(*arguments: Any, **options: Any) -> list[dict[str, Any]]:
        culprit = {"id": "culprit", "environment_changes": ["environment:FLAG"]}

        return [culprit, {"id": "victim", "passed": False, "error": "TypeError: other\n  at test_x.py:9"}]

    monkeypatch.setattr(execution, "in_order", in_order)
    error = "ValueError: original\n  at test_x.py:7"
    causes, _ = environment_causes(Adapter(), "test_x.py", "victim", ["culprit"], 1, error)  # type: ignore[arg-type]
    assert [(cause.baseline, cause.repair, cause.confirmed) for cause in causes] == [
        (["different_failure"] * 2, [], False),
    ]


def test_stand_ins_of_a_group_wide_culprit_are_not_other_causes(monkeypatch: pytest.MonkeyPatch) -> None:
    class Grouped(Adapter):
        def group(self, id: str) -> str | None:
            return "Group" if id.startswith("group_") else None

    def order(adapter: Any, path: str, seed: int) -> list[str]:
        return ["group_a", "group_b", "group_c", "victim"]

    def in_order(adapter: Any, path: str, ids: list[str], seed: int) -> list[dict[str, Any]]:
        # A fixture of the group breaks the victim, whichever of its tests ran
        return [{"id": "victim", "passed": not any(id.startswith("group_") for id in ids)}]

    monkeypatch.setattr(execution, "order", order)
    monkeypatch.setattr(execution, "in_order", in_order)
    culprits = Bisect(Grouped(), "test_x.py", 1)("victim")  # type: ignore[arg-type]
    assert culprits.group_wide == culprits.ids
    assert culprits.others == []


def test_another_set_of_culprits_is_marked_group_wide(monkeypatch: pytest.MonkeyPatch) -> None:
    class Grouped(Adapter):
        def group(self, id: str) -> str | None:
            return "Group" if id.startswith("group_") else None

    def order(adapter: Any, path: str, seed: int) -> list[str]:
        return ["group_a", "group_b", "group_c", "plain", "victim"]

    def in_order(adapter: Any, path: str, ids: list[str], seed: int) -> list[dict[str, Any]]:
        # A test of its own and a fixture of the group break the victim, each without the other
        return [{"id": "victim", "passed": not any(id == "plain" or id.startswith("group_") for id in ids)}]

    monkeypatch.setattr(execution, "order", order)
    monkeypatch.setattr(execution, "in_order", in_order)
    culprits = Bisect(Grouped(), "test_x.py", 1)("victim")  # type: ignore[arg-type]
    named = [*culprits.ids, *(id for ids in culprits.others for id in ids)]
    assert sorted(id[:6] for id in named) == ["group_", "plain"]
    assert [id for id in named if id.startswith("group_")] == culprits.group_wide


def test_runs_that_never_reach_the_victim_tell_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    def order(adapter: Any, path: str, seed: int) -> list[str]:
        return ["hangs"]

    def in_order(adapter: Any, path: str, ids: list[str], seed: int) -> list[dict[str, Any]]:
        return [{"id": "hangs", "passed": False, "timed_out": True}]

    monkeypatch.setattr(execution, "order", order)
    monkeypatch.setattr(execution, "in_order", in_order)
    culprits = Bisect(Adapter(), "test_x.py", 1)("victim")  # type: ignore[arg-type]
    assert (culprits.ids, culprits.broken) == ([], True)


def test_group_tests_breaking_the_victim_each_their_way_are_named(monkeypatch: pytest.MonkeyPatch) -> None:
    class Grouped(Adapter):
        def group(self, id: str) -> str | None:
            return "Group" if id.startswith("group_") else None

    def order(adapter: Any, path: str, seed: int) -> list[str]:
        return ["plain", "group_a", "group_b", "group_c", "victim"]

    def in_order(adapter: Any, path: str, ids: list[str], seed: int) -> list[dict[str, Any]]:
        return [{"id": "victim", "passed": not {"plain", "group_a", "group_b"} & set(ids)}]

    monkeypatch.setattr(execution, "order", order)
    monkeypatch.setattr(execution, "in_order", in_order)
    culprits = Bisect(Grouped(), "test_x.py", 1)("victim")  # type: ignore[arg-type]
    named = sorted([*culprits.ids, *(id for ids in culprits.others for id in ids)])
    assert named == ["group_a", "group_b", "plain"]


def test_a_test_another_set_took_stands_in_for_no_culprit(monkeypatch: pytest.MonkeyPatch) -> None:
    class Grouped(Adapter):
        def group(self, id: str) -> str | None:
            return "Group"

    def in_order(adapter: Any, path: str, ids: list[str], seed: int) -> list[dict[str, Any]]:
        return [{"id": "victim", "passed": not {"found", "other", "target"} & set(ids)}]

    monkeypatch.setattr(execution, "in_order", in_order)
    bisect = Bisect(Grouped(), "test_x.py", 1)  # type: ignore[arg-type]
    bisect.order = ["found", "other", "passing", "target", "victim"]
    assert bisect.group_wide_of(["target"], "victim") == ["target"]
    assert bisect.group_wide_of(["target"], "victim", ["found"]) == []


def test_a_victim_that_fails_with_no_test_before_it_has_no_culprits(monkeypatch: pytest.MonkeyPatch) -> None:
    def order(adapter: Any, path: str, seed: int) -> list[str]:
        return ["a", "victim"]

    def in_order(adapter: Any, path: str, ids: list[str], seed: int) -> list[dict[str, Any]]:
        return [{"id": "victim", "passed": False}]

    monkeypatch.setattr(execution, "order", order)
    monkeypatch.setattr(execution, "in_order", in_order)
    culprits = Bisect(Adapter(), "test_x.py", 1)("victim")  # type: ignore[arg-type]
    assert (culprits.ids, culprits.alone) == ([], True)


def test_runs_that_write_no_results_break_the_bisect(monkeypatch: pytest.MonkeyPatch) -> None:
    def order(adapter: Any, path: str, seed: int) -> list[str]:
        return ["a", "victim"]

    def in_order(adapter: Any, path: str, ids: list[str], seed: int) -> list[dict[str, Any]]:
        raise execution.NoResults("test_x.py", "", "crashed")

    monkeypatch.setattr(execution, "order", order)
    monkeypatch.setattr(execution, "in_order", in_order)
    monkeypatch.setattr(execution, "retrying", lambda attempt: attempt())
    culprits = Bisect(Adapter(), "test_x.py", 1)("victim")  # type: ignore[arg-type]
    assert (culprits.ids, culprits.broken) == ([], True)


@pytest.mark.parametrize(
    ("victim", "outcome"),
    [
        (None, "not_run"),
        ({"skipped": True}, "skipped"),
        ({"passed": False, "timed_out": True}, "interrupted"),
        ({"passed": False, "expected_failure": True}, "expected_failure"),
    ],
)
def test_what_became_of_the_victim_in_a_probe_is_kept(
    monkeypatch: pytest.MonkeyPatch,
    victim: dict[str, Any] | None,
    outcome: str,
) -> None:
    def in_order(*arguments: Any, **options: Any) -> list[dict[str, Any]]:
        culprit = {"id": "culprit", "environment_changes": ["environment:FLAG"]}

        return [culprit] + ([] if victim is None else [{"id": "victim", **victim}])

    monkeypatch.setattr(execution, "in_order", in_order)
    causes, _ = environment_causes(Adapter(), "test_x.py", "victim", ["culprit"], 1, "boom")  # type: ignore[arg-type]
    assert [cause.baseline for cause in causes] == [[outcome] * 2]


def test_a_probe_that_writes_no_results_measures_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    def in_order(*arguments: Any, **options: Any) -> list[dict[str, Any]]:
        raise execution.NoResults("test_x.py", "", "crashed")

    monkeypatch.setattr(execution, "in_order", in_order)
    assert environment_causes(Adapter(), "test_x.py", "victim", ["culprit"], 1, "boom") == ([], False)  # type: ignore[arg-type]


def test_skipped_victim_does_not_pin_a_culprit(monkeypatch):
    search = Bisect(Adapter(), "test_x.py", 0)
    monkeypatch.setattr(
        search,
        "reaching",
        lambda tests, victim: [{"id": victim, "passed": False} if tests else {"id": victim, "skipped": True}],
    )
    assert search.pinned(["gate"], "victim") == []
