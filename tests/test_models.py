import io
import tempfile
import time

from deflake.investigation import bisect_seeds
from deflake.models import OUTSIDE, CulpritLeak, Culprits, Diagnosis, EnvironmentCause, Run, Summary, key
from deflake.reports.text import TextReport, culprit_line, percent


def run(seed: int, passed: bool, error: str | None = None, **fields: object) -> Run:
    return Run(seed=seed, passed=passed, error=None if passed else error or "boom", **fields)  # type: ignore[arg-type]


def test_summary_counts_runs_and_failures() -> None:
    summary = Summary([run(1, True), run(2, False), run(3, False)], id="t.py::test_a", name="test_a")
    assert (summary.total, summary.failures) == (3, 2)
    assert summary.failure_rate == 2 / 3


def test_summary_without_runs_has_no_failure_rate() -> None:
    assert Summary([], id="t.py::test_a").failure_rate == 0.0


def test_errors_are_grouped_by_message_with_their_seeds() -> None:
    summary = Summary(
        [run(1, False, "AssertionError: 1 != 2"), run(2, True), run(3, False, "AssertionError: 1 != 2")],
        id="t",
    )
    assert summary.errors == {"AssertionError: 1 != 2": [1, 3]}


def test_errors_that_differ_only_in_object_addresses_are_one_error() -> None:
    summary = Summary(
        [
            run(1, False, "AssertionError: <User object at 0x10a2b3c4d> is not None"),
            run(2, False, "AssertionError: <User object at 0x7f0011223344> is not None"),
        ],
        id="t",
    )
    assert summary.errors == {"AssertionError: <User object at 0x...> is not None": [1, 2]}


def test_other_hexadecimal_numbers_are_kept() -> None:
    summary = Summary([run(1, False, "hash 0xdeadbeef"), run(2, False, "hash 0xfeedface")], id="t")
    assert list(summary.errors) == ["hash 0xdeadbeef", "hash 0xfeedface"]


def test_runs_with_a_group_show_the_error_of_the_first_one() -> None:
    summary = Summary(
        [
            run(1, False, "crashed, output: a", group="crashed"),
            run(2, False, "crashed, output: b", group="crashed"),
        ],
        id="t",
    )
    assert summary.errors == {"crashed, output: a": [1, 2]}


def test_leaks_count_runs_where_the_test_skipped_itself_too() -> None:
    summary = Summary(
        [run(1, True, leaks=["FOO was set"]), run(2, True, leaks=["FOO was set", "cwd changed"])],
        id="t",
        skipped=1,
        skipped_leaks=["FOO was set"],
    )
    assert summary.leaks == {"FOO was set": 3, "cwd changed": 1}


def diagnosis(
    file_runs: Summary,
    isolated: Summary | None = None,
    culprits: Culprits | None = None,
) -> Diagnosis:
    return Diagnosis(name="test_a", id="t", file_runs=file_runs, isolated_runs=isolated, culprits=culprits)


def test_verdicts() -> None:
    failing = Summary([run(1, False)], id="t")
    passing = Summary([run(1, True)], id="t")
    found = Culprits(ids=["t2"], command="cmd")
    nothing = Culprits(ids=[], command="cmd")
    assert diagnosis(passing).verdict == "stable"
    assert diagnosis(failing).verdict == "failed"
    assert diagnosis(failing, failing).verdict == "fails_on_its_own"
    assert diagnosis(failing, passing).verdict == "order_dependent"
    assert diagnosis(failing, passing, found).verdict == "order_dependent"
    assert diagnosis(failing, passing, nothing).verdict == "random"
    assert (
        diagnosis(failing, passing, Culprits(ids=[], command="cmd", alone=True)).verdict == "fails_on_its_own"
    )


def test_clean_means_stable_and_no_leaks() -> None:
    assert diagnosis(Summary([run(1, True)], id="t")).clean
    assert not diagnosis(Summary([run(1, True, leaks=["x"])], id="t")).clean
    assert not diagnosis(Summary([run(1, False)], id="t")).clean


def test_culprit_names_default_to_their_ids() -> None:
    assert Culprits(ids=["a", "b"], command="cmd").names == ["a", "b"]
    assert OUTSIDE == "(outside of tests)"


def test_a_leak_counts_once_in_a_run() -> None:
    summary = Summary([run(1, True, leaks=["x", "x"]), run(2, True, leaks=["x"])], id="t")
    assert summary.leaks == {"x": 2}


def test_a_crash_alone_is_the_crash_of_the_file_whatever_the_output() -> None:
    crash = "the test process timed out after 5 seconds during this test"
    file_runs = Summary([run(1, False, f"{crash}\n.. output", group=crash)], id="t")
    alone = Summary([run(2, False, f"{crash}\n. output", group=crash)], id="t")
    assert bisect_seeds(Diagnosis(name="t", id="t", file_runs=file_runs, isolated_runs=alone)) == []


def test_an_error_never_seen_alone_is_bisected() -> None:
    file_runs = Summary([run(1, False, "AssertionError: 1"), run(2, False, "KeyError: 'a'")], id="t")
    alone = Summary([run(3, False, "AssertionError: 1")], id="t")
    assert bisect_seeds(Diagnosis(name="t", id="t", file_runs=file_runs, isolated_runs=alone)) == [2]


def test_percent_rounds_half_up_and_never_hides_a_failure() -> None:
    assert [percent(rate) for rate in (0.004, 0.005, 0.125, 0.145, 0.5, 0.995, 1.0)] == [
        "<1%",
        "1%",
        "13%",
        "15%",
        "50%",
        ">99%",
        "100%",
    ]


def test_errors_that_differ_only_in_the_folder_of_tmp_path_are_one_error() -> None:
    summary = Summary(
        [
            run(1, False, "AssertionError: missing /tmp/pytest-of-me/pytest-12/test_a0/data.json"),
            run(2, False, "AssertionError: missing /tmp/pytest-of-me/pytest-13/test_a1/data.json"),
        ],
        id="t",
    )
    assert summary.errors == {"AssertionError: missing /tmp/pytest-of-me/pytest-N/test_aN/data.json": [1, 2]}


def test_errors_that_differ_only_in_mock_ids_or_temporary_names_are_one_error() -> None:
    temp = tempfile.gettempdir()
    summary = Summary(
        [
            run(1, False, f"assert <MagicMock id='4495833744'> == 5 in {temp}/tmpk3_9az1q/x"),
            run(2, False, f"assert <MagicMock id='4471020048'> == 5 in {temp}/tmp0a1b2c3d/x"),
        ],
        id="t",
    )
    assert summary.errors == {f"assert <MagicMock id='N'> == 5 in {temp}/tmpXXXXXXXX/x": [1, 2]}


def test_temporary_names_with_a_prefix_or_suffix_of_their_own_are_one_error() -> None:
    temp = tempfile.gettempdir()
    summary = Summary(
        [
            run(1, False, f"{temp}/tmpjquwxasg.json: missing, {temp}/app-ruhh0gjb: empty"),
            run(2, False, f"{temp}/tmpsw4wwx1d.json: missing, {temp}/app-0a1b2c3d: empty"),
        ],
        id="t",
    )
    assert summary.errors == {f"{temp}/tmpXXXXXXXX.json: missing, {temp}/app-XXXXXXXX: empty": [1, 2]}


def test_errors_that_differ_only_in_the_order_of_a_set_are_one_error() -> None:
    first = "assert {'b', 'a'} == {'c'}\nE  Extra items in the left set:\nE  'b'\nE  'a'"
    second = "assert {'a', 'b'} == {'c'}\nE  Extra items in the left set:\nE  'a'\nE  'b'"
    summary = Summary([run(1, False, first), run(2, False, second)], id="t")
    assert list(summary.errors.values()) == [[1, 2]]


def test_a_temporary_folder_inside_a_path_is_not_the_one_of_tempfile() -> None:
    temp = tempfile.gettempdir()
    summary = Summary(
        [
            run(1, False, f"missing /srv{temp}/report_a1.csv"),
            run(2, False, f"missing /srv{temp}/report_b2.csv"),
        ],
        id="t",
    )
    assert len(summary.errors) == 2


def test_errors_of_unittest_that_differ_only_in_the_order_of_a_set_are_one_error() -> None:
    header = "AssertionError: Items in the first set but not the second:\n"
    first = header + "b'beta'\n'gamma'\n('alpha', 1)\nItems in the second set but not the first:\n'delta'"
    second = header + "('alpha', 1)\n'gamma'\nb'beta'\nItems in the second set but not the first:\n'delta'"
    counts = "AssertionError: Element counts were not equal:\nFirst has 1, Second has 0:  'b'\n"
    summary = Summary(
        [
            run(1, False, first),
            run(2, False, second),
            run(3, False, counts + "First has 1, Second has 0:  'a'"),
            run(4, False, counts.replace("'b'", "'a'") + "First has 1, Second has 0:  'b'"),
        ],
        id="t",
    )
    assert list(summary.errors.values()) == [[1, 2], [3, 4]]


def test_errors_with_a_set_pytest_cut_short_are_one_error() -> None:
    def message(items: str) -> str:
        return (
            "assert {'user0', 'us... 'user5', ...} == set()\n  Extra items in the left set:\n"
            f"{items}  ...\n  \n  ...Full output truncated (3 lines hidden), use '-vv' to show"
        )

    summary = Summary(
        [run(1, False, message("  'user2'\n  'user4'\n")), run(2, False, message("  'user1'\n  'user3'\n"))],
        id="t",
    )
    assert list(summary.errors.values()) == [[1, 2]]


def test_sets_of_any_items_are_sorted_wherever_they_are_nested() -> None:
    assert key("assert frozenset({('b', 1), ('a', 2)}) == {<A object at 0x10a2b3c4d>, <A x=1>}") == (
        "assert frozenset({('a', 2), ('b', 1)}) == {<A object at 0xN>, <A x=1>}"
    )
    assert key("got [{'b', 'a'}, {'d', 'c'}]") == "got [{'a', 'b'}, {'c', 'd'}]"


def test_dicts_and_text_that_only_looks_like_brackets_are_kept() -> None:
    for message in ("{'b': 1, 'a': 2}", "a < b, c > d {'it's", "f({x, y)", "{'a, b', 'c'"):
        assert key(message) == message


def test_a_long_message_with_brackets_that_never_close_is_normalized_quickly() -> None:
    started = time.monotonic()
    key("(<a{[" * 20_000)
    assert time.monotonic() - started < 2


def same_key(*messages: str) -> bool:
    return len({key(message) for message in messages}) == 1


def test_objects_in_a_repr_pytest_cut_short_are_one_error() -> None:
    assert same_key(
        "assert [<test_o.Orde... 0x10b5fc990>] == [<test_o.Orde... 0x10ba1d4d0>]",
        "assert [<test_o.Orde... 0x10c0011aa>] == [<test_o.Orde... 0x10d0022bb>]",
    )


def test_set_items_of_any_kind_listed_one_a_line_are_one_error() -> None:
    first = "datetime.date(2024, 1, 3)\nPosixPath('/b')\nItem(name='n1')\nfrozenset({'x'})\nNone"
    second = "None\nfrozenset({'x'})\nItem(name='n1')\nPosixPath('/b')\ndatetime.date(2024, 1, 3)"
    header = "assert {...} == set()\n  Extra items in the left set:\n"
    assert same_key(header + first, header + second)
    assert not same_key(header + first, header + second.replace("n1", "n2"))


def test_a_set_explanation_cut_short_at_an_item_is_one_error() -> None:
    def message(item: str) -> str:
        return (
            "assert {'user0', 'us... 'user5', ...} == {'user10', 'u... 'user3', ...}\n"
            f"  Extra items in the right set:\n  'user1'\n  '{item}'...\n  \n"
            "  ...Full output truncated (3 lines hidden), use '-vv' to show"
        )

    assert same_key(message("user12"), message("user13"))


def test_a_repr_unittest_cut_short_is_one_error() -> None:
    assert same_key(
        "AssertionError: {'key0': {...}, 'key[138 chars]m0'}} != {'key0': {'a'}}",
        "AssertionError: {'key0': {...}, 'key[138 chars]x1'}} != {'key0': {'a'}}",
    )


def test_the_marks_of_a_diff_and_pytest_full_diff_are_left_out() -> None:
    first = (
        "{'admin': {'read', 'write', 'delete'}} != {'admin': {'read', 'write'}}\n"
        "- {'admin': {'read', 'write', 'delete'}}\n?                   ----------\n"
    )
    second = (
        "{'admin': {'delete', 'read', 'write'}} != {'admin': {'write', 'read'}}\n"
        "- {'admin': {'delete', 'read', 'write'}}\n?            ----------\n"
    )
    assert same_key(first, second)
    assert same_key(
        "assert a == b\n  Full diff:\n  - {\n  -   B(),\n  -   A(),",
        "assert a == b\n  Full diff:\n  - {\n  -   A(),",
    )


def test_an_apostrophe_before_a_set_is_not_a_string() -> None:
    assert same_key("ValueError: user's roles {'b', 'a'}", "ValueError: user's roles {'a', 'b'}")


def test_an_error_is_shown_as_the_first_run_had_it() -> None:
    summary = Summary(
        [
            run(1, False, "assert {'b', 'a'} == {<A at 0x10a2b3c4d>}"),
            run(2, False, "assert {'a', 'b'} == {<A at 0x7f0011223344>}"),
        ],
        id="t",
    )
    assert summary.errors == {"assert {'b', 'a'} == {<A at 0x...>}": [1, 2]}


def test_an_ellipsis_of_the_data_is_not_a_cut() -> None:
    first = "AssertionError: {'status': 'pending...', 'id': 5} != {'status': 'pending...', 'id': 6}"
    assert not same_key(first, first.replace("5", "7", 1))
    assert not same_key("assert 'step 1...' == 'done'", "assert 'step 2...' == 'done'")
    assert not same_key("TimeoutError: waiting for postgres...", "TimeoutError: waiting for redis...")
    assert same_key("assert {'user0', 'user5', ...} == set()", "assert {'user1', 'user3', ...} == set()")


def test_the_full_diff_of_newer_pytest_is_left_out() -> None:
    header = "assert a == b\n  Full diff: (-: missing in left side, +: extra in left side)\n"
    assert same_key(header + "  - {\n  -   B(),\n  -   A(),", header + "  - {\n  -   A(),")


def test_a_set_saferepr_cut_inside_an_item_is_one_error() -> None:
    assert same_key(
        "assert {<Perm.READ: ...ITE: 'write'>} == {<Perm.READ: 'read'>}",
        "assert {<Perm.ADMIN:...ITE: 'write'>} == {<Perm.READ: 'read'>}",
    )
    assert same_key(
        "assert {User(name='a...role='staff')} == set()",
        "assert {User(name='c...role='admin')} == set()",
    )
    assert same_key(
        "Differing items:\n{'a': {Cust(kind='regular-customer-ac...)}} != {'a': set()}",
        "Differing items:\n{'a': {Cust(kind='vip-customer-bb...)}} != {'a': set()}",
    )


def test_a_cut_is_one_error_whatever_quotes_it_takes_away() -> None:
    extra = "\n  Extra items in the left set:\n  <Perm.ADMIN: 'owner-owner'>"
    assert same_key(
        "assert {<Perm.ADMIN:...owner-owner'>} == {<Perm.READ: 'read'>}" + extra,
        "assert {<Perm.OWNER:...READ: 'read'>} == {<Perm.READ: 'read'>}" + extra,
    )
    assert same_key(
        "{'a': {'x', 'y', 'item-7-'[546 chars]': 1} != {'a': {'q'}, 'b': 1}",
        "{'a': {'y', 'x', 'item-30-[546 chars]': 1} != {'a': {'q'}, 'b': 1}",
    )


def test_a_cut_inside_a_string_with_brackets_is_one_error() -> None:
    assert same_key(
        "AssertionError: {'a': {'call(23)', 'call(10)', 'call([431 chars]': 1} != {'a': {'q'}, 'b': 1}",
        "AssertionError: {'a': {'call(1)', 'call(13)', 'call(39[431 chars]': 1} != {'a': {'q'}, 'b': 1}",
    )
    assert same_key(
        "AssertionError: {'a': {'[2, 3]', '[5, 6]', '[9[431 chars]}} != {'a': {'q'}}",
        "AssertionError: {'a': {'[1, 2]', '[7[431 chars]}} != {'a': {'q'}}",
    )
    assert same_key(
        "assert {'call(23)', 'call(1...'call(5)'} == set()",
        "assert {'call(7)', 'cal...'call(2)'} == set()",
    )


def test_a_repr_cut_before_its_set_is_one_error() -> None:
    assert same_key(
        "AssertionError: assert Spy(calls={'g...ser_4)'}, n=0) == Spy(calls=set(), n=0)",
        "AssertionError: assert Spy(calls={'g...ser_0)'}, n=0) == Spy(calls=set(), n=0)",
    )
    assert same_key(
        "Tuples differ: ({'c25)', 'c22)', [186 chars])'},) != (set(),)",
        "Tuples differ: ({'c3)', 'c17)', [186 chars])'},) != (set(),)",
    )
    assert same_key(
        "assert User(name='bo...or', 'admin'}) == User(name='bo...les={'admin'})",
        "assert User(name='bo...or', 'owner'}) == User(name='bo...les={'admin'})",
    )


def test_each_set_of_culprits_that_breaks_the_test_on_its_own_is_shown() -> None:
    culprits = Culprits(ids=["a"], command="cmd", others=[["b", "c"]])
    assert culprit_line(culprits, "order_dependent") == "culprit: a\n  or, just as well: b, c"


def test_culprits_not_pinned_down_and_runs_that_broke_are_told_apart() -> None:
    summary = Summary([run(1, False)], id="t.py::test_a")
    alone = Summary([run(1, True)], id="t.py::test_a")
    broken = Culprits(ids=[], command="cmd", broken=True)
    diagnosis = Diagnosis("test_a", "t.py::test_a", summary, alone, broken)
    assert diagnosis.verdict == "failed"
    assert culprit_line(broken, diagnosis.verdict) == "culprit: not found, every run broke before the test"

    reproduced = Culprits(ids=[], command="cmd", reproduced=True)
    diagnosis = Diagnosis("test_a", "t.py::test_a", summary, alone, reproduced)
    assert diagnosis.verdict == "random"
    assert "came back with the same order but not with fewer" in culprit_line(reproduced, diagnosis.verdict)


def test_the_same_cause_outcome_with_several_seeds_is_shown_once() -> None:
    summary = Summary([run(1, False), run(2, False)], id="t.py::test_a", name="test_a")
    alone = Summary([run(3, True)], id="t.py::test_a")
    outcome = ["different_failure"] * 2, [], []
    causes = [EnvironmentCause("t.py::test_b", "FLAG", seed, *outcome) for seed in (1, 2)]
    culprits = Culprits(ids=["t.py::test_b"], command="cmd")
    out = io.StringIO()
    TextReport(out, io.StringIO()).print(
        [Diagnosis("test_a", "t.py::test_a", summary, alone, culprits, causes)],
        file="t.py",
        not_run=[],
        shared=[],
    )
    assert out.getvalue().count("environment cause: FLAG") == 1
    assert "environment cause: FLAG from t.py::test_b: not confirmed (seeds 1, 2)\n" in out.getvalue()


def test_runs_that_all_broke_in_the_bisect_show_the_runs_alone() -> None:
    summary = Summary([run(1, False)], id="t.py::test_a", name="test_a")
    alone = Summary([run(3, True)], id="t.py::test_a")
    culprits = Culprits(ids=[], command="cmd", broken=True)
    out = io.StringIO()
    TextReport(out, io.StringIO()).print(
        [Diagnosis("test_a", "t.py::test_a", summary, alone, culprits)],
        file="t.py",
        not_run=[],
        shared=[],
    )
    assert "  verdict: failed (alone: failed 0 of 1 runs (0%))\n" in out.getvalue()


def test_many_leaks_of_a_culprit_not_checked_are_counted() -> None:
    summary = Summary([run(1, False)], id="t.py::test_a", name="test_a")
    alone = Summary([run(3, True)], id="t.py::test_a")
    culprits = Culprits(ids=["t.py::test_b"], command="cmd")
    leaks = [
        CulpritLeak("test_b", f"the level of logger 'app{n}' was changed", False, False) for n in range(8)
    ]
    out = io.StringIO()
    TextReport(out, io.StringIO()).print(
        [Diagnosis("test_a", "t.py::test_a", summary, alone, culprits, culprit_leaks=leaks)],
        file="t.py",
        not_run=[],
        shared=[],
    )
    assert out.getvalue().count("(not checked as the cause)") == 5
    assert "  test_b leaks 3 more not checked as the cause\n" in out.getvalue()


def test_a_numbered_patch_is_named_as_its_leak() -> None:
    cause = EnvironmentCause("a", "mock.patch of json.dumps #2", 1, [], [], [], "patch")
    assert cause.label == "mock.patch of json.dumps"
