"""Prints diagnoses in a form meant for people"""

from __future__ import annotations

import math
from collections import Counter
from typing import TextIO

from deflake.models import OUTSIDE, Culprits, Diagnosis, EnvironmentCause, Summary, Verdict, shown

VERDICTS: dict[Verdict, str] = {
    "stable": "stable",
    "failed": "failed, could not run it alone",
    "fails_on_its_own": "fails on its own",
    "order_dependent": "fails only after other tests",
    "random": "fails at random, not after particular tests",
}

# How many leaks of a culprit no experiment tried are shown
SHOWN = 5


class TextReport:
    def __init__(self, out: TextIO, err: TextIO) -> None:
        self.out = out
        self.started = False
        self.shared_names: list[str] = []
        self.printed: list[str] = []

    def start(self, file: str, number: int, total: int) -> None:
        if self.started:
            self.line()

        self.started = True
        self.line(f"== {file} ({number} of {total})")

    def not_checked(self, file: str, message: str) -> None:
        """The error is already on stderr"""

    def flush(self) -> None:
        self.out.flush()

    def finish(self) -> None:
        pass

    def print(self, diagnoses: list[Diagnosis], *, file: str, not_run: list[str], shared: list[str]) -> None:
        """shared: names more than one test of the file has, even tests left out of the report"""
        counts = Counter(diagnosis.name for diagnosis in diagnoses)
        self.shared_names = [name for name, count in counts.items() if count > 1] + shared
        for diagnosis in diagnoses:
            self.diagnosis(diagnosis)

    def line(self, text: str = "") -> None:
        self.out.write(text + "\n")

    def diagnosis(self, diagnosis: Diagnosis) -> None:
        self.line(f"{self.heading(diagnosis)}: {outcome(diagnosis)}")
        self.leaks(diagnosis.file_runs)
        if diagnosis.file_runs.marked:
            self.line(f"  {marked(diagnosis)}")

        if diagnosis.verdict == "stable":
            return

        self.verdict(diagnosis)
        if diagnosis.culprits is not None:
            self.culprits(diagnosis.culprits, diagnosis.verdict)

        # The same outcome with several seeds is one block
        outcomes: dict[tuple[str, ...], list[EnvironmentCause]] = {}
        for cause in diagnosis.causes:
            same = (
                cause.detector,
                cause.key,
                cause.culprit,
                *cause.baseline,
                "",
                *cause.repair,
                "",
                *cause.reapply,
            )
            outcomes.setdefault(same, []).append(cause)

        for causes in outcomes.values():
            cause = causes[0]
            status = "confirmed" if cause.confirmed else "not confirmed"
            seeds = [str(each.seed) for each in causes]
            seen = f"seed {seeds[0]}" if len(seeds) == 1 else f"seeds {', '.join(seeds)}"
            self.line(f"  {cause.detector} cause: {cause.key} from {cause.culprit}: {status} ({seen})")
            self.line(
                f"    baseline: {', '.join(cause.baseline)}; "
                f"repair: {', '.join(cause.repair) or 'not run'}; "
                f"reapply: {', '.join(cause.reapply) or 'not run'}"
            )

        # A logging config changes loggers by the hundred: the first few tell enough
        unchecked: dict[str, list[str]] = {}
        for leak in diagnosis.culprit_leaks:
            if not leak.checked:
                unchecked.setdefault(leak.culprit, []).append(leak.message)

        for culprit, messages in unchecked.items():
            for message in messages[:SHOWN]:
                self.line(f"  {culprit} leaks: {message} (not checked as the cause)")
            if len(messages) > SHOWN:
                self.line(f"  {culprit} leaks {len(messages) - SHOWN} more not checked as the cause")

        self.errors(diagnosis.file_runs)
        if diagnosis.isolated_runs is not None and alone_errors(diagnosis):
            self.errors(diagnosis.isolated_runs, "alone, ")

    def heading(self, diagnosis: Diagnosis) -> str:
        if diagnosis.name in self.shared_names:
            return f"{diagnosis.name} ({diagnosis.id})"

        return diagnosis.name

    def verdict(self, diagnosis: Diagnosis) -> None:
        verdict = VERDICTS[diagnosis.verdict]
        if diagnosis.isolated_runs is None:
            self.line(f"  verdict: {verdict}")
        elif diagnosis.verdict == "failed":
            self.line(f"  verdict: failed (alone: {failed(diagnosis.isolated_runs)})")
        else:
            self.line(f"  verdict: {verdict} (alone: {failed(diagnosis.isolated_runs)})")

    def culprits(self, culprits: Culprits, verdict: Verdict) -> None:
        self.line(f"  {culprit_line(culprits, verdict)}")
        self.line(f"  reproduce: {culprits.command}")

    def leaks(self, summary: Summary) -> None:
        for leak, count in summary.leaks.items():
            self.line(f"  leaks: {leak} ({count} of {summary.total + summary.skipped} runs)")

    def errors(self, summary: Summary, prefix: str = "") -> None:
        """Runs alone show only the errors the runs of the file did not have"""
        groups = summary.failed_runs()
        if prefix:
            groups = {group: runs for group, runs in groups.items() if group not in self.printed}

        for runs in groups.values():
            error = shown(runs[0].error)
            self.line(f"  {prefix}seeds {', '.join(str(run.seed) for run in runs)}:")
            self.line("\n".join(f"    {line}" if line else line for line in error.split("\n")))

        self.printed = list(groups)


def marked(diagnosis: Diagnosis) -> str:
    if diagnosis.file_runs.failures:
        return "marked flaky: reruns hide these failures in the project's own runs"

    return "marked flaky, but did not fail here: the marker may be out of date"


def outcome(diagnosis: Diagnosis) -> str:
    """Errors outside of tests that only leaked failed in no run, which says nothing"""
    runs = diagnosis.file_runs
    if diagnosis.id == OUTSIDE and runs.failures == 0:
        return f"none in {runs.total} runs, but leaks"

    return failed(runs)


def failed(summary: Summary) -> str:
    """Runs where the test skipped itself are not counted, but said"""
    if summary.total == 0:
        return f"skipped in all {summary.skipped} runs"

    skipped = f", skipped in {summary.skipped} more" if summary.skipped else ""

    return f"failed {summary.failures} of {summary.total} runs ({percent(summary.failure_rate)}){skipped}"


def percent(rate: float) -> str:
    """A rare failure is not 0%, and a failure in all but a few runs is not 100%"""
    if 0 < rate < 0.005:
        return "<1%"
    if 0.995 <= rate < 1:
        return ">99%"

    # Half up, as people round: 12.5% is 13%. Rounded first, so 14.5 stored as 14.4999… is 15
    return f"{math.floor(round(rate * 100, 6) + 0.5)}%"


def culprit_line(culprits: Culprits, verdict: Verdict) -> str:
    if culprits.alone:
        return "fails with no other test before it with this seed"

    if culprits.found:
        line = f"culprit: {', '.join(culprits.names or [])}"
        for names in culprits.other_names or []:
            line += f"\n  or, just as well: {', '.join(names)}"

        return line

    if culprits.broken:
        return "culprit: not found, every run broke before the test"
    if culprits.reproduced:
        return "culprit: not found, the failure came back with the same order but not with fewer tests"
    if verdict == "random":
        return "culprit: none, the failure did not happen again with the same seed and order"

    return "culprit: not found, the failure did not happen again with the same seed"


def alone_errors(diagnosis: Diagnosis) -> bool:
    """Whether the runs alone had errors the runs of the file did not"""
    alone = diagnosis.isolated_runs
    if alone is None or alone.failures == 0:
        return False

    return bool(set(alone.error_groups) - set(diagnosis.file_runs.error_groups))
