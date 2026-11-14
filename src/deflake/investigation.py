"""Runs a test file many times, then runs every failing test on its own to tell a flaky test from one
broken by its neighbours, and bisects the latter"""

from __future__ import annotations

from collections import Counter
from dataclasses import replace

from deflake import execution
from deflake.adapters import Adapter
from deflake.bisect import Bisect, environment_causes
from deflake.models import OUTSIDE, CulpritLeak, Culprits, Diagnosis, EnvironmentCause, Summary, key, shape
from deflake.repeat import Repeat

# How many failing seeds bisect tries before it gives up
SEEDS = 3


class Investigation:
    """selector: look only at the tests on this line (digits) or with this name, but still run the
    whole file"""

    def __init__(
        self,
        adapter: Adapter,
        path: str,
        *,
        runs: int,
        selector: str | None = None,
        jobs: int = 1,
    ) -> None:
        self.adapter = adapter
        self.path = path
        self.runs = runs
        self.selector = selector
        self.jobs = jobs
        self.names: dict[str, str] = {}
        # Every test of the file, selected or not: a culprit may be any of them
        self.summaries: dict[str, Summary] = {}
        # Ids of the selected tests that never started in any run
        self.not_run: list[str] = []

    def __call__(self) -> list[Diagnosis]:
        """One diagnosis per test, sorted by name. Empty when every test skipped itself. With a
        selector, errors outside of tests still show up: they may be why the test never ran"""
        # Before the runs, so a selector without a test is reported right away
        focus = self.focus() if self.selector else None
        summaries = self.focused(focus)
        ordered = sorted(summaries.values(), key=lambda summary: (summary.name, summary.id))

        return [self.diagnose(summary) for summary in ordered]

    @property
    def shared_names(self) -> list[str]:
        """Names more than one test of the file has, so a report tells them apart by their ids"""
        return [name for name, count in Counter(self.names.values()).items() if count > 1]

    def focus(self) -> list[str]:
        assert self.selector is not None
        listed = execution.retrying(lambda: execution.listed(self.adapter, self.path))

        return self.adapter.focus(self.path, self.selector, listed)

    def focused(self, focus: list[str] | None) -> dict[str, Summary]:
        file = self.repeat()
        summaries = file()
        self.summaries = summaries
        self.names = file.names
        self.not_run = [id for id in focus if id not in file.seen] if focus else []

        if focus is None:
            return summaries

        return {id: summary for id, summary in summaries.items() if id in focus or id == OUTSIDE}

    def repeat(self, only: list[str] | None = None) -> Repeat:
        return Repeat(self.adapter, self.path, runs=self.runs, only=only, jobs=self.jobs)

    def diagnose(self, summary: Summary) -> Diagnosis:
        diagnosis = Diagnosis(name=summary.name, id=summary.id, file_runs=summary)
        # Errors outside of tests cannot run on their own
        if summary.failures == 0 or summary.id == OUTSIDE:
            return diagnosis

        diagnosis = replace(diagnosis, isolated_runs=self.isolated(summary))
        seeds = bisect_seeds(diagnosis)

        if not seeds:
            return diagnosis

        culprits = self.bisect(summary, seeds)
        diagnosis = replace(diagnosis, culprits=culprits)
        if culprits.found:
            causes = self.causes(summary, culprits, seeds)
            diagnosis = replace(diagnosis, causes=causes, culprit_leaks=self.culprit_leaks(culprits, causes))

        return diagnosis

    def culprit_leaks(self, culprits: Culprits, causes: list[EnvironmentCause]) -> list[CulpritLeak]:
        """What the culprits leaked: one no experiment tried is next to the failure, not proven its cause"""
        result = []
        for id in culprits.ids:
            name = self.label(id, False)
            summary = self.summaries.get(id)
            for message in summary.leaks if summary else {}:
                tried = [
                    cause for cause in causes if cause.culprit == id and message.startswith(cause.label + " ")
                ]
                result.append(
                    CulpritLeak(name, message, bool(tried), any(cause.confirmed for cause in tried))
                )

        return result

    def causes(self, summary: Summary, culprits: Culprits, seeds: list[int]) -> list[EnvironmentCause]:
        """State the culprits leave changed, tried as the cause seed by seed until one is confirmed"""
        found: list[EnvironmentCause] = []
        for seed in seeds:
            original = next(run for run in summary.runs if run.seed == seed and not run.passed)
            if original.group:
                continue
            causes, reached = environment_causes(
                self.adapter,
                self.path,
                summary.id,
                culprits.ids,
                seed,
                key(original.error),
            )
            found += causes
            if any(cause.confirmed for cause in causes):
                break

            # Nothing these experiments change was left changed, as with a list changed in place: the
            # leak is of another kind, and other seeds would only repeat the same processes. A run that
            # never reached the victim tells nothing, so another seed is tried then
            if not causes and reached:
                break

        return found

    def isolated(self, summary: Summary) -> Summary | None:
        """None when no run alone reached the test: whether it fails on its own is not known"""
        alone = self.repeat(only=[summary.id])
        try:
            alone()
        except execution.NoResults:
            return None

        return alone.all.get(summary.id)

    def bisect(self, summary: Summary, seeds: list[int]) -> Culprits:
        """Tries the seeds one by one until bisect finds culprits, or finds the test failing alone"""
        culprits = None
        # The first seed that broke the victim again: its command shows the failure
        reproduced: Culprits | None = None
        # Broken only when no seed told anything of the failure
        broken = True
        for seed in seeds:
            original = next(run for run in summary.runs if run.seed == seed and not run.passed)
            error = original.group or key(original.error)
            culprits = Bisect(self.adapter, self.path, seed, error)(summary.id)
            if culprits.found or culprits.alone:
                break
            reproduced = reproduced or (culprits if culprits.reproduced else None)
            broken = broken and culprits.broken

        assert culprits is not None
        if not (culprits.found or culprits.alone):
            culprits = replace(reproduced or culprits, reproduced=reproduced is not None, broken=broken)

        wide = set(culprits.group_wide)

        return replace(
            culprits,
            names=[self.label(id, id in wide) for id in culprits.ids],
            other_names=[[self.label(id, id in wide) for id in ids] for ids in culprits.others],
        )

    def label(self, id: str, group_wide: bool) -> str:
        """Tests with the same name are told apart by their ids"""
        name = self.names.get(id, id)
        if list(self.names.values()).count(name) > 1:
            name = f"{name} ({id})"

        return (
            f"{name} or any other test of its group (a fixture or hook of the group?)" if group_wide else name
        )


def bisect_seeds(diagnosis: Diagnosis) -> list[int]:
    """Failing seeds worth a bisect. A test that fails on its own may also fail after other tests,
    with an error it never had alone: its seeds are bisected too"""
    errors = diagnosis.file_runs.errors
    if diagnosis.verdict == "order_dependent":
        return seeds_of(errors)

    if diagnosis.verdict == "fails_on_its_own" and diagnosis.isolated_runs is not None:
        alone = {shape(error) for error in diagnosis.isolated_runs.error_groups}
        groups = diagnosis.file_runs.error_groups
        return seeds_of({error: seeds for error, seeds in groups.items() if shape(error) not in alone})

    return []


def seeds_of(errors: dict[str, list[int]]) -> list[int]:
    """The first seed of every error first, as a single odd failure may be the first one"""
    firsts = [seeds[0] for seeds in errors.values()]
    every = [seed for seeds in errors.values() for seed in seeds]

    return list(dict.fromkeys(firsts + every))[:SEEDS]
