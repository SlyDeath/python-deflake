"""Finds the smallest set of tests that make a test fail when they run before it.

A failure counts only when it repeats: a test that fails at random would otherwise get culprits that
have nothing to do with it
"""

from __future__ import annotations

import math
from collections.abc import Callable

from deflake import execution
from deflake.adapters import Adapter
from deflake.models import Culprits, EnvironmentCause, same_failure

# How many runs in a row a failure must happen in to count
CONFIRM = 2
# How many other tests of a group must stand in for a culprit
STAND_INS = 2
# How many times a run that never reached the victim, as when another test hung, is tried again
REACH = 2
# How many runs without a culprit must all pass for the culprit to be needed
NEEDED = 2
# How many more sets of culprits are looked for among the tests the found ones leave out
OTHERS = 2
# How many changes of one culprit are tried as the cause: each takes four more runs
CANDIDATES = 20
# Kinds tried first, by the names leaks.states.kinds gives them; the switches of loggers come last,
# as one logging config changes them by the hundred
FIRST = ("environment", "module", "patch", "clock", "django", "process")


class Bisect:
    def __init__(self, adapter: Adapter, path: str, seed: int, error: str | None = None) -> None:
        self.adapter = adapter
        self.path = path
        self.seed = seed
        # How the victim failed with this seed: another failure is not the one to find culprits of
        self.error = error
        self.alone = False
        # The failure came back with the order of the file, but no culprits were pinned down
        self.reproduced = False
        # Every run broke before it reached the victim
        self.broken = False
        # Some run got to the victim
        self.reached = False
        self.group_wide: list[str] = []
        self.others: list[list[str]] = []
        self.order: list[str] = []

    def __call__(self, victim: str) -> Culprits:
        ids = self.culprits(victim)

        return Culprits(
            ids=ids,
            command=self.adapter.reproduce(self.path, self.seed),
            alone=self.alone,
            group_wide=self.group_wide,
            others=self.others,
            reproduced=self.reproduced and not ids,
            broken=self.broken,
        )

    def culprits(self, victim: str) -> list[str]:
        """Culprit ids, or none if the failure does not reproduce or happens alone"""
        try:
            for _ in range(REACH + 1):
                self.order = execution.retrying(lambda: execution.order(self.adapter, self.path, self.seed))
                if victim in self.order:
                    break
        except execution.NoResults:
            # The process broke before its first test every time, so nothing is known of the failure
            self.broken = True
            return []

        before = self.order[: self.order.index(victim)] if victim in self.order else self.order
        if not self.fails(before, victim, CONFIRM):
            # Every run stopped before the victim, as when a test before it hung
            self.broken = not self.reached
            return []

        if self.fails([], victim, CONFIRM):
            self.alone = True
            return []

        self.reproduced = True
        culprits = self.pinned(before, victim)
        if culprits:
            self.group_wide = self.group_wide_of(culprits, victim)
            self.others = self.others_of(before, culprits, victim)

        return culprits

    def pinned(self, candidates: list[str], victim: str) -> list[str]:
        """Smaller steps ran once each, so the end result must break the victim again. A victim that
        fails only now and then can also pass a step by chance and keep a test that has nothing to do
        with it: without each culprit the victim must run without that failure every time"""
        culprits = minimize(candidates, lambda tests: self.fails(tests, victim))
        if not culprits or not self.fails(culprits, victim, CONFIRM):
            return []

        needed = [
            culprit
            for culprit in culprits
            if self.without_original_failure([c for c in culprits if c != culprit], victim, NEEDED)
        ]
        if needed == culprits:
            return culprits
        if not needed or not self.fails(needed, victim, CONFIRM):
            return []

        return needed

    def others_of(self, before: list[str], culprits: list[str], victim: str) -> list[list[str]]:
        """Other tests that break the victim on their own just as well: naming one of two would hide
        the other, and which one is named would change from run to run"""
        # The stand-ins of a culprit of its group bring the same state: they are not other causes
        groups = {self.adapter.group(culprit) for culprit in self.group_wide} - {None}
        taken = set(culprits) | {id for id in before if self.adapter.group(id) in groups}
        result: list[list[str]] = []
        for _ in range(OTHERS):
            rest = [id for id in before if id not in taken]
            if not rest or not self.fails(rest, victim, CONFIRM):
                break
            found = self.pinned(rest, victim)
            if not found:
                break
            result.append(found)
            # As for the culprits: other tests of a group would only stand in for its fixture
            wide = self.group_wide_of(found, victim, sorted(taken))
            self.group_wide.extend(wide)
            shared = {self.adapter.group(id) for id in wide} - {None}
            taken.update(found, (id for id in before if self.adapter.group(id) in shared))

        return result

    def fails(self, tests: list[str], victim: str, times: int = 1) -> bool:
        """Only this victim counts, failing the way it was seen to: other tests may fail too, a skipped
        victim did not fail, and neither did one stopped by the timeout, which a shorter list of tests
        would not reach. Another failure of the victim would bring culprits of another failure"""
        for _ in range(times):
            rows = self.reaching(tests, victim)
            if not any(row["id"] == victim and self.failed(row) for row in rows):
                return False

        return True

    def without_original_failure(self, tests: list[str], victim: str, times: int) -> bool:
        """Check the victim reached a recorded outcome other than the target failure in every run"""
        for _ in range(times):
            rows = self.reaching(tests, victim)
            row = next((row for row in rows if row["id"] == victim), {})
            if row.get("passed") is None or row.get("skipped") or row.get("timed_out") or self.failed(row):
                return False

        return True

    def failed(self, row: execution.Row) -> bool:
        if row.get("passed") is not False or row.get("timed_out"):
            return False

        return self.error is None or same_failure(self.error, row.get("error"), row.get("group"), self.path)

    def reaching(self, tests: list[str], victim: str) -> list[execution.Row]:
        """The rows of a run that got to the victim: one stopped before it says nothing about it"""
        rows: list[execution.Row] = []
        for _ in range(REACH + 1):
            try:
                rows = execution.retrying(
                    lambda: execution.in_order(self.adapter, self.path, [*tests, victim], self.seed)
                )
            except execution.NoResults:
                return []
            if any(row["id"] == victim for row in rows):
                self.reached = True
                break

        return rows

    def group_wide_of(self, culprits: list[str], victim: str, taken: list[str] | None = None) -> list[str]:
        """A culprit that other tests of its group replace with the same failure is not to blame
        itself: a fixture or hook of the group is. Two of them must, as one may break the victim on
        its own. taken: tests other sets of culprits took, which stand in for nothing"""
        result = []
        for culprit in culprits:
            stand_ins = self.stand_ins(culprit, [*culprits, victim, *(taken or [])])
            if len(stand_ins) == STAND_INS and all(
                self.fails(self.in_order([c for c in culprits if c != culprit] + [stand_in]), victim, CONFIRM)
                for stand_in in stand_ins
            ):
                result.append(culprit)

        return result

    def stand_ins(self, culprit: str, taken: list[str]) -> list[str]:
        group = self.adapter.group(culprit)
        if group is None:
            return []

        return [id for id in self.order if id not in taken and self.adapter.group(id) == group][:STAND_INS]

    def in_order(self, ids: list[str]) -> list[str]:
        return sorted(ids, key=self.order.index)


def minimize(candidates: list[str], fails: Callable[[list[str]], bool]) -> list[str]:
    """Drops ever smaller chunks of candidates while the failure still happens"""
    size = math.ceil(len(candidates) / 2)
    while size > 0:
        previous = len(candidates)
        chunks = [candidates[start : start + size] for start in range(0, len(candidates), size)]
        for chunk in chunks:
            rest = [id for id in candidates if id not in chunk]
            if fails(rest):
                candidates = rest
        # Removing a cleanup can make an earlier candidate unnecessary
        if size > 1 or len(candidates) == previous:
            size //= 2

    return candidates


def environment_causes(
    adapter: Adapter,
    path: str,
    victim: str,
    culprits: list[str],
    seed: int,
    error: str,
) -> tuple[list[EnvironmentCause], bool]:
    """Paired interventions after a culprit's teardown, on every kind of state a probe can put back at
    once; values stay in the child process. Also whether every baseline run measured the culprit and
    reached the victim, so no cause found means the culprits changed no state"""
    result = []
    reached = True
    for culprit in culprits:

        def attempt(mode: str, name: str = "", culprit: str = culprit) -> tuple[str, list[str] | None]:
            """The outcome for the victim, and the keys the culprit changed: None when it was not measured"""
            try:
                rows = execution.in_order(
                    adapter,
                    path,
                    [*culprits, victim],
                    seed,
                    probe={"culprit": culprit, "mode": mode, "key": name},
                )
            except execution.NoResults:
                return "not_run", None

            source = next((row for row in rows if row["id"] == culprit), {})
            changes = source.get("environment_changes")
            if any(row.get("process_interrupted") for row in rows):
                return "interrupted", changes
            if mode != "baseline" and not source.get("intervened"):
                return "not_applied", changes

            row = next((row for row in rows if row["id"] == victim), {})
            if not row:
                return "not_run", changes
            if row.get("skipped"):
                return "skipped", changes
            if row.get("group") or row.get("timed_out"):
                return "interrupted", changes
            if row.get("expected_failure"):
                return "expected_failure", changes
            if row.get("passed") is True:
                return "passed", changes

            same = same_failure(error, row.get("error"), row.get("group"), path)

            return ("same_failure" if same else "different_failure"), changes

        first, changes = attempt("baseline")
        second, again = attempt("baseline")
        measured = changes is not None and again is not None
        reached = reached and measured and not {first, second} & {"not_run", "interrupted"}
        for full in sorted(set(changes or []) & set(again or []), key=priority)[:CANDIDATES]:
            detector, _, name = full.partition(":")
            baseline = [first, second]
            repair: list[str] = []
            reapply: list[str] = []
            if baseline == ["same_failure"] * 2:
                repair = [attempt("repair", full)[0] for _ in range(2)]
                reapply = [attempt("reapply", full)[0] for _ in range(2)]
            result.append(EnvironmentCause(culprit, name, seed, baseline, repair, reapply, detector))

    return result, reached


def priority(key: str) -> tuple[int, bool, str]:
    """Earlier kinds first, and within a kind the switches of loggers last"""
    kind, _, name = key.partition(":")
    rank = FIRST.index(kind) if kind in FIRST else len(FIRST)

    return (rank, " of logger " in name or " of the root logger" in name, name)
