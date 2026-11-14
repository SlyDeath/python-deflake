import io

from deflake.models import Culprits, Diagnosis, Run, Summary
from deflake.reports.text import TextReport, culprit_line


def run(seed: int, passed: bool, error: str | None = None) -> Run:
    return Run(seed=seed, passed=passed, error=None if passed else error or "boom")


def test_culprits_without_a_test_before_are_told_apart_by_the_verdict() -> None:
    assert culprit_line(Culprits(ids=[], command="cmd", alone=True), "fails_on_its_own") == (
        "fails with no other test before it with this seed"
    )
    nothing = Culprits(ids=[], command="cmd")
    assert culprit_line(nothing, "random") == (
        "culprit: none, the failure did not happen again with the same seed and order"
    )
    assert (
        culprit_line(nothing, "failed")
        == "culprit: not found, the failure did not happen again with the same seed"
    )


def test_a_flaky_marker_is_told_whether_the_test_failed() -> None:
    failing = Summary([run(1, False)], id="t.py::test_a", name="test_a", marked=True)
    passing = Summary([run(1, True)], id="t.py::test_b", name="test_b", marked=True)
    out = io.StringIO()
    TextReport(out, io.StringIO()).print(
        [Diagnosis("test_a", "t.py::test_a", failing), Diagnosis("test_b", "t.py::test_b", passing)],
        file="t.py",
        not_run=[],
        shared=[],
    )
    assert "  marked flaky: reruns hide these failures in the project's own runs\n" in out.getvalue()
    assert "  marked flaky, but did not fail here: the marker may be out of date\n" in out.getvalue()


def test_tests_of_one_name_are_told_apart_and_errors_alone_are_shown_once() -> None:
    first = Summary([run(1, False, "boom")], id="t.py::A::test_a", name="test_a")
    second = Summary([run(1, False, "boom")], id="t.py::B::test_a", name="test_a")
    alone = Summary([run(2, False, "boom"), run(3, False, "other")], id="t.py::A::test_a")
    out = io.StringIO()
    TextReport(out, io.StringIO()).print(
        [
            Diagnosis("test_a", "t.py::A::test_a", first, alone),
            Diagnosis("test_a", "t.py::B::test_a", second),
        ],
        file="t.py",
        not_run=[],
        shared=[],
    )
    assert "test_a (t.py::A::test_a): " in out.getvalue()
    assert "test_a (t.py::B::test_a): " in out.getvalue()
    assert out.getvalue().count("boom") == 2
    assert "alone, " in out.getvalue()
