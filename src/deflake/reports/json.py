"""Prints diagnoses as JSON for CI and other tools. The format may change before 1.0"""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any, TextIO

from deflake.models import Diagnosis, Summary


class JsonReport:
    def __init__(self, out: TextIO, err: TextIO) -> None:
        self.out = out
        self.err = err
        self.objects: list[dict[str, Any]] = []

    def start(self, file: str, number: int, total: int) -> None:
        """Nothing is printed until the end, so the progress goes to stderr"""
        self.err.write(f"deflake: checking {file} ({number} of {total})\n")

    def not_checked(self, file: str, message: str) -> None:
        self.objects.append(
            {
                "file": file,
                **never_ran(file),
                "verdict": "not_checked",
                "errors": [{"message": message, "seeds": []}],
            }
        )

    def flush(self) -> None:
        self.out.flush()

    def print(self, diagnoses: list[Diagnosis], *, file: str, not_run: list[str], shared: list[str]) -> None:
        """not_run: ids of selected tests that never started, each one with the verdict "not_run\""""
        objects = [as_json(diagnosis) for diagnosis in diagnoses] + [never_ran(id) for id in not_run]
        self.objects.extend({"file": file, **item} for item in objects)

    def finish(self) -> None:
        """One array for every file, printed at the end"""
        self.out.write(json.dumps(self.objects, indent=2, ensure_ascii=False) + "\n")


def as_json(diagnosis: Diagnosis) -> dict[str, Any]:
    runs = diagnosis.file_runs

    return {
        "name": diagnosis.name,
        "id": diagnosis.id,
        "verdict": diagnosis.verdict,
        "runs": counts(runs),
        "isolated_runs": counts(diagnosis.isolated_runs) if diagnosis.isolated_runs else None,
        "errors": [{"message": message, "seeds": seeds} for message, seeds in runs.errors.items()],
        "leaks": [{"message": message, "runs": count} for message, count in runs.leaks.items()],
        "culprits": asdict(diagnosis.culprits) if diagnosis.culprits else None,
        "marked_flaky": runs.marked,
        "causes": [{**asdict(cause), "confirmed": cause.confirmed} for cause in diagnosis.causes],
        "culprit_leaks": [asdict(leak) for leak in diagnosis.culprit_leaks],
    }


def never_ran(id: str) -> dict[str, Any]:
    return {
        "name": id,
        "id": id,
        "verdict": "not_run",
        "runs": counts(Summary([], id=id)),
        "isolated_runs": None,
        "errors": [],
        "leaks": [],
        "culprits": None,
        "marked_flaky": False,
        "causes": [],
        "culprit_leaks": [],
    }


def counts(summary: Summary) -> dict[str, Any]:
    """Not rounded: one failure in thousands of runs is not 0"""
    return {
        "total": summary.total,
        "failures": summary.failures,
        "failure_rate": summary.failure_rate,
        "skipped": summary.skipped,
    }
