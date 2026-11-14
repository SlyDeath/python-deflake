from typing import Any, cast

import pytest

from deflake import investigation
from deflake.adapters import Adapter
from deflake.models import Culprits, Diagnosis, EnvironmentCause, Run, Summary

SUMMARY = Summary([Run(seed, passed=False, error="boom") for seed in (1, 2, 3)], id="victim")


def tried(
    monkeypatch: pytest.MonkeyPatch,
    causes: list[EnvironmentCause],
    *,
    reached: bool = True,
    summary: Summary = SUMMARY,
) -> list[int]:
    """The seeds tried for causes when every try finds these"""
    seeds: list[int] = []

    def environment_causes(*arguments: Any) -> tuple[list[EnvironmentCause], bool]:
        seeds.append(arguments[4])

        return causes, reached

    monkeypatch.setattr(investigation, "environment_causes", environment_causes)
    # The adapter and the file are passed on to the patched experiments only
    checked = investigation.Investigation(cast(Adapter, object()), "test_file.py", runs=1)
    checked.causes(summary, Culprits(["culprit"], "cmd"), [1, 2, 3])

    return seeds


def test_no_changed_key_ends_the_causes_at_the_first_seed(monkeypatch: pytest.MonkeyPatch) -> None:
    assert tried(monkeypatch, []) == [1]


def test_an_unconfirmed_cause_moves_on_to_the_next_seed(monkeypatch: pytest.MonkeyPatch) -> None:
    cause = EnvironmentCause("culprit", "FLAG", 1, ["different_failure"] * 2, [], [])
    assert tried(monkeypatch, [cause]) == [1, 2, 3]


def test_a_run_short_of_the_victim_moves_on_to_the_next_seed(monkeypatch: pytest.MonkeyPatch) -> None:
    assert tried(monkeypatch, [], reached=False) == [1, 2, 3]


def test_a_confirmed_cause_ends_the_causes(monkeypatch: pytest.MonkeyPatch) -> None:
    confirmed = ["same_failure"] * 2, ["passed"] * 2, ["same_failure"] * 2
    cause = EnvironmentCause("culprit", "FLAG", 1, *confirmed)
    assert tried(monkeypatch, [cause]) == [1]


def test_a_seed_whose_error_changes_from_run_to_run_is_not_tried(monkeypatch: pytest.MonkeyPatch) -> None:
    runs = [Run(1, passed=False, error="boom", group="boom"), *SUMMARY.runs[1:]]
    assert tried(monkeypatch, [], summary=Summary(runs, id="victim")) == [2]


def test_a_test_failing_at_random_with_other_numbers_is_not_bisected() -> None:
    file = Summary([Run(1, passed=False, error="assert 0.61 < 0.5"), Run(2, passed=True)], id="victim")
    alone = Summary([Run(3, passed=False, error="assert 0.73 < 0.5")], id="victim")
    assert investigation.bisect_seeds(Diagnosis("victim", "victim", file, alone)) == []


def test_an_error_a_test_never_had_alone_is_bisected() -> None:
    file = Summary([Run(1, passed=False, error="KeyError: 'user'")], id="victim")
    alone = Summary([Run(3, passed=False, error="assert 0.73 < 0.5")], id="victim")
    assert investigation.bisect_seeds(Diagnosis("victim", "victim", file, alone)) == [1]


def test_an_error_at_another_line_a_test_never_had_alone_is_bisected() -> None:
    file = Summary([Run(1, passed=False, error="assert 3 == 1\n  at test_x.py:14")], id="victim")
    alone = Summary([Run(3, passed=False, error="assert 0 == 1\n  at test_x.py:10")], id="victim")
    assert investigation.bisect_seeds(Diagnosis("victim", "victim", file, alone)) == [1]


def test_an_error_about_another_name_with_a_number_is_bisected() -> None:
    file = Summary([Run(1, passed=False, error="KeyError: 'user2'")], id="victim")
    alone = Summary([Run(3, passed=False, error="KeyError: 'user1'")], id="victim")
    assert investigation.bisect_seeds(Diagnosis("victim", "victim", file, alone)) == [1]


def test_another_set_of_culprits_is_named_with_its_group(monkeypatch: pytest.MonkeyPatch) -> None:
    found = Culprits(["plain"], "cmd", group_wide=["grouped"], others=[["grouped"]])
    monkeypatch.setattr(investigation, "Bisect", lambda *arguments: lambda id: found)
    checked = investigation.Investigation(cast(Adapter, object()), "test_file.py", runs=1)
    culprits = checked.bisect(SUMMARY, [1])
    label = "grouped or any other test of its group (a fixture or hook of the group?)"
    assert culprits.other_names == [[label]]


def test_a_seed_that_reached_the_victim_outweighs_a_broken_one(monkeypatch: pytest.MonkeyPatch) -> None:
    broken = Culprits([], "cmd", broken=True)
    results = iter([broken, Culprits([], "seed 2", reproduced=True), Culprits([], "seed 3")])
    monkeypatch.setattr(investigation, "Bisect", lambda *arguments: lambda id: next(results))
    checked = investigation.Investigation(cast(Adapter, object()), "test_file.py", runs=1)
    culprits = checked.bisect(SUMMARY, [1, 2, 3])
    # The command is of the seed the failure came back with
    assert (culprits.broken, culprits.reproduced, culprits.command) == (False, True, "seed 2")
