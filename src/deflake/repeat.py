"""Runs a test file many times, each in a fresh process with a new seed, and sums up every test"""

from __future__ import annotations

import contextlib
import queue
import random
import threading
from collections import Counter
from collections.abc import Callable
from typing import Any

from deflake import execution
from deflake.adapters import Adapter, Error
from deflake.models import OUTSIDE, Run, Summary

Row = dict[str, Any]

# Seeds every runner accepts
SEEDS = 0xFFFF


class Repeat:
    """only: run just these tests, by id; jobs: how many processes at once"""

    def __init__(
        self,
        adapter: Adapter,
        path: str,
        *,
        runs: int,
        only: list[str] | None = None,
        jobs: int = 1,
    ) -> None:
        if runs < 1 or jobs < 1:
            raise ValueError("runs and jobs must be positive")

        self.adapter = adapter
        self.path = path
        self.runs = runs
        self.only = only
        self.jobs = jobs
        # Ids of every test that started in the last call, even those left out of its result
        self.seen: list[str] = []
        # Every summary of the last call, the quiet ones too, and every test's name by its id
        self.all: dict[str, Summary] = {}
        self.names: dict[str, str] = {}

    def __call__(self) -> dict[str, Summary]:
        """Test id => Summary. Runs where a test skipped itself count only for its leaks, and a test
        that skipped itself every time and leaked nothing is left out. Errors outside of any test,
        in a run of the whole file, show up as OUTSIDE"""
        rows = in_parallel(seeds(self.runs), self.jobs, self.execute)
        self.check_some_ran(rows)
        self.check_some_tests(rows)
        groups: dict[str, list[Row]] = {}
        for row in rows:
            groups.setdefault(row["id"], []).append(row)

        self.seen = list(groups)
        self.all = {id: summarize(id, group) for id, group in groups.items()}
        self.names = {id: summary.name for id, summary in self.all.items()}

        return {id: summary for id, summary in self.all.items() if not quiet(id, summary)}

    def execute(self, seed: int, job: int) -> list[Row]:
        env = {execution.JOB_KEY: str(job)} if self.jobs > 1 else {}
        if self.only is None:
            return execution.whole_file(self.adapter, self.path, seed, env)

        return execution.some(self.adapter, self.path, seed, self.only, env)

    def check_some_ran(self, rows: list[Row]) -> None:
        """A run that broke before its first test is reported among the others, unless every run did"""
        silent = [row for row in rows if "output" in row]
        if silent and len(silent) == len(rows):
            raise execution.NoResults(self.path, silent[0]["output"], silent[0]["ending"])

    def check_some_tests(self, rows: list[Row]) -> None:
        """A file without tests, or whose tests a filter leaves out, is a mistake: a passing report
        would hide it"""
        if self.only is not None:
            return
        if any(row["id"] != OUTSIDE or row.get("ran") for row in rows):
            return
        if all(row["passed"] and not row["leaks"] for row in rows):
            raise Error(f"no tests ran in {self.path} (no tests in it, or a filter leaves them all out)")


def seeds(count: int) -> list[int]:
    """Each run gets a seed of its own while there are enough of them"""
    if count > SEEDS:
        return [random.randrange(SEEDS) for _ in range(count)]

    return random.sample(range(SEEDS), count)


def in_parallel(items: list[int], jobs: int, work: Callable[[int, int], list[Row]]) -> list[Row]:
    """Threads only wait for the test processes. On an error or an interrupt the test processes of
    the other threads are killed"""
    todo: queue.Queue[int] = queue.Queue()
    for item in items:
        todo.put(item)

    results: list[list[Row]] = []
    errors: list[BaseException] = []
    lock = threading.Lock()
    count = min(jobs, len(items))
    # Waited for instead of joining the threads: a join an interrupt broke may hang on Python 3.11
    # and 3.12
    done = [threading.Event() for _ in range(count)]

    def worker(job: int) -> None:
        try:
            work_through(job)
        finally:
            done[job - 1].set()

    def work_through(job: int) -> None:
        while not errors:
            try:
                item = todo.get_nowait()
            except queue.Empty:
                return
            try:
                rows = work(item, job)
            except BaseException as error:
                with lock:
                    errors.append(error)
                execution.stop_all()
                return
            with lock:
                results.append(rows)

    execution.resume()
    for job in range(1, count + 1):
        threading.Thread(target=worker, args=(job,), daemon=True).start()

    try:
        for event in done:
            while not event.wait(0.1):
                pass
    except BaseException as error:
        errors.append(KeyboardInterrupt())
        stop()
        for event in done:
            # Another Ctrl-C must not leave the processes and their temporary folders behind
            while True:
                try:
                    event.wait()
                    break
                except (KeyboardInterrupt, RuntimeError):
                    stop()
        # Python 3.11 can break the lock of Event.wait with Ctrl-C, and the interrupt comes out as this
        if isinstance(error, RuntimeError):
            raise KeyboardInterrupt from error
        raise

    if errors:
        raise errors[0]

    return [row for rows in results for row in rows]


def stop() -> None:
    """Another Ctrl-C while processes start kills the running ones at once: the jobs are still waited for"""
    with contextlib.suppress(KeyboardInterrupt):
        execution.stop_all()


def quiet(id: str, summary: Summary) -> bool:
    """Leaks outside of tests, such as those of a module fixture, are not quiet either"""
    if id == OUTSIDE:
        return summary.failures == 0 and not summary.leaks

    return summary.total == 0 and not summary.leaks


def summarize(id: str, rows: list[Row]) -> Summary:
    skipped = [row for row in rows if row.get("skipped")]
    ran = [row for row in rows if not row.get("skipped")]
    runs = [
        Run(
            seed=row["seed"],
            passed=bool(row["passed"]),
            error=row.get("error"),
            leaks=list(row.get("leaks") or []),
            group=row.get("group"),
        )
        for row in ran
    ]

    return Summary(
        runs,
        id=id,
        name=Counter(row["name"] for row in rows).most_common(1)[0][0],
        skipped=len(skipped),
        skipped_leaks=[leak for row in skipped for leak in dict.fromkeys(row.get("leaks") or [])],
        marked=any(row.get("flaky") for row in rows),
    )
