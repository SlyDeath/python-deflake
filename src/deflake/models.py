"""What deflake learns about tests: single runs, their summary and the diagnosis of a test"""

from __future__ import annotations

import os
import re
import tempfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

# Id of the row that stands for errors outside of any test, such as a failing collection or hook
OUTSIDE = "(outside of tests)"

Verdict = Literal["stable", "failed", "fails_on_its_own", "order_dependent", "random"]

# An error that happens again shows parts that change from one test process to another. They are
# replaced, before items whose order changes are sorted, so the order sorted is the same too

# Python prints objects without a __repr__ of their own as "<module.Class object at 0x7f...>", and
# the address changes in every process. pytest may cut the repr short before " at "
ADDRESS = re.compile(r"( at |\.\.\. )0x[0-9a-fA-F]{6,16}(?=>)|( at )0x[0-9a-fA-F]{6,16}")
# The id in the repr of a mock, as with ADDRESS
MOCK_ID = re.compile(r"( id=')\d+(')")
# The folder of pytest's tmp_path, numbered anew in every test process
PYTEST_TEMP = re.compile(r"(pytest-of-[^/\\]+[/\\]pytest-)\d+")
# The folder of a test in it, numbered by how many before it had the same name
PYTEST_TEST = re.compile(r"(pytest-of-[^/\\]+[/\\]pytest-N[/\\][^/\\\s'\"]*?)\d+(?=[/\\\s'\",):]|$)")
# Where tempfile makes its files, as the test process sees it
TEMPDIRS = sorted({tempfile.gettempdir(), os.path.realpath(tempfile.gettempdir())}, key=len, reverse=True)
# A name tempfile makes up at random in it, between the prefix and the suffix of the caller
TEMPFILE = re.compile(
    # The folder starts a path, not a part of another one
    rf"(?<![^\s'\"(=:])((?:{'|'.join(map(re.escape, TEMPDIRS))})[/\\][^/\\\s'\"]*?)"
    rf"[a-z0-9_]{{8}}(?=[/\\\s'\",):.]|$)"
)
VOLATILE = [
    (MOCK_ID, r"\g<1>N\g<2>"),
    (PYTEST_TEMP, r"\g<1>N"),
    (PYTEST_TEST, r"\g<1>N"),
    (TEMPFILE, r"\g<1>XXXXXXXX"),
]

# Items of a set come in the order of their hashes, which every process seeds anew for strings and
# takes from the addresses for objects. pytest and unittest list the items one set has and the
# other does not, one a line, after a header
SET_HEADER = re.compile(r"[ \t]*(?:E[ \t]+)?.*(?:set|but not the (?:first|second)):[ \t]*")
# Lines that end such a list
AFTER_ITEMS = re.compile(r"[ \t]*(?:E[ \t]*)?(?:$|Use -v|Full diff|Omitting|\.\.\.|.*:[ \t]*$)")
# As unittest's assertCountEqual lists the items counted differently
COUNTS = re.compile(r"(?:[ \t]*(?:E[ \t]+)?First has \d+, Second has \d+:[^\n]*(?:\n|$))+")
# Numbers in an error, addresses included
NUMBERS = re.compile(r"(?<![\w.])(?:0x[0-9a-fA-F]+|\d+(?:\.\d+)?)")
# Where an error says it happened, and the name of its kind, as an exception or a plain assert
LOCATION = "\n  at "
KIND = re.compile(r"[\w.]+")
# pytest's full diff, a pretty print in the order of the hashes, and the marks of difflib under a
# line, which point at characters of the order
FULL_DIFF = re.compile(r"\n[ \t]*(?:E[ \t]+)?Full diff:[^\n]*\n.*", re.DOTALL)
MARKS = re.compile(r"^[ \t]*(?:E[ \t]+)?\?[ \t].*\n?", re.MULTILINE)
# Where pytest cuts an explanation short, keeping items the order of the hashes chooses
TRUNCATED = re.compile(r"Full output truncated")
# Where unittest cuts a repr short. pytest and reprlib put ... in place of what they leave out
CHARS = re.compile(r"\[\d+ chars\]")
CLOSING = {"{": "}", "[": "]", "(": ")", "<": ">"}


@dataclass(frozen=True)
class Run:
    """Result of a single test run, with messages about the global state it leaked.

    group: what the error is grouped by instead of its text, when the text changes from run to run
    """

    seed: int
    passed: bool
    error: str | None = None
    leaks: list[str] = field(default_factory=list)
    group: str | None = None


class Summary:
    """All runs of a single test. Runs where the test skipped itself are only counted in skipped"""

    def __init__(
        self,
        runs: list[Run],
        *,
        id: str,
        name: str | None = None,
        skipped: int = 0,
        skipped_leaks: list[str] | None = None,
        marked: bool = False,
    ) -> None:
        self.runs = runs
        self.id = id
        self.name = name if name is not None else id
        self.skipped = skipped
        self.skipped_leaks = skipped_leaks or []
        # Marked flaky, so the project's regular runs rerun it when it fails
        self.marked = marked

    @property
    def total(self) -> int:
        return len(self.runs)

    @property
    def failures(self) -> int:
        return sum(1 for run in self.runs if not run.passed)

    @property
    def failure_rate(self) -> float:
        return self.failures / self.total if self.total else 0.0

    @property
    def errors(self) -> dict[str, list[int]]:
        """Error message => seeds of the runs that failed with it.

        Messages that differ only in what changes from one process to another are one error, shown
        as the first run had it. Runs with a group, such as a crash whose output depends on the
        seed, show the error of the first one too
        """
        errors: dict[str, list[int]] = {}
        for runs in self.failed_runs().values():
            errors.setdefault(shown(runs[0].error), []).extend(run.seed for run in runs)

        return errors

    @property
    def error_groups(self) -> dict[str, list[int]]:
        """As errors, by the group of the runs: the same crash in runs of the file and runs alone,
        whatever output the runs had"""
        return {key: [run.seed for run in runs] for key, runs in self.failed_runs().items()}

    def failed_runs(self) -> dict[str, list[Run]]:
        groups: dict[str, list[Run]] = {}
        for run in self.runs:
            if not run.passed:
                groups.setdefault(run.group or key(run.error), []).append(run)

        return groups

    @property
    def leaks(self) -> dict[str, int]:
        """Leak message => in how many runs it happened, the skipped ones included"""
        return dict(
            Counter([leak for run in self.runs for leak in dict.fromkeys(run.leaks)] + self.skipped_leaks)
        )


def shown(message: str | None) -> str:
    """The error as it is shown: with addresses and other names made up anew in every process
    replaced"""
    return volatile(message, "0x...")


def key(message: str | None) -> str:
    """What runs that failed the same way have in common. The order of the hashes, which changes
    from one process to another, decides the order of the items of a set and which of them a cut
    short repr or explanation keeps: those are sorted or left out"""
    message = volatile(message, "0xN")
    message = MARKS.sub("", FULL_DIFF.sub("", message))
    truncated = bool(TRUNCATED.search(message))
    lines = [cut_line(line) for line in set_items(message.split("\n"), truncated)]
    message = Brackets("\n".join(lines), collapse=True).sorted()

    return COUNTS.sub(lambda found: sorted_lines(found[0]), message)


def shape(error: str) -> str:
    """Numbers left out: a test that fails at random tells other values every time. Bisecting it would
    name whatever tests happen to run before it"""
    message, at, location = error.rpartition(LOCATION)

    return f"{NUMBERS.sub('N', message)}{at}{location}" if at else NUMBERS.sub("N", error)


def same_failure(observed: str, error: str | None, group: str | None = None, file: str | None = None) -> bool:
    """Whether a run failed the way it was seen to fail: of the same kind at the same line of the test
    file. Its values may differ, as the state an assert shows, which other tests before it also change.
    A line of another file may be a library's that every failure of a kind passes through"""
    other = group or key(error)
    if shape(observed) == shape(other):
        return True

    seen = point(observed)
    if seen is None or file is None or seen[1].rpartition(":")[0] != Path(file).name:
        return False

    return seen == point(other)


def point(error: str) -> tuple[str, str] | None:
    """The kind of an error and the line it happened at, None for one that tells no line"""
    message, at, location = error.rpartition(LOCATION)
    if not at:
        return None

    kind = KIND.match(message.lstrip())

    return (kind.group() if kind else "", location)


def cut_line(line: str) -> str:
    """A line with a repr cut short, as pytest and unittest cut a long one, as what comes before
    its first bracket or string. Which items it keeps, and which brackets and quotes the cut takes
    away, depends on the order of the hashes. An ellipsis inside a string with its brackets in
    place is text, as in 'pending...'"""
    if CHARS.search(line) or ("..." in line and not whole(line)):
        found = re.search(r"[(\[{'\"]", line)
        return line[: found.start()] + "<cut>" if found else line

    return line


def whole(line: str) -> bool:
    """Every ... in strings, and every bracket closed by its own"""
    opened: list[str] = []
    index = 0
    while index < len(line):
        char = line[index]
        if char in "'\"" and starts_string(line, index):
            end = string_end(line, index)
            if line[end - 1 : end] != char or end == index + 1:
                return False
            index = end
            continue
        if line.startswith("...", index):
            return False
        if char in "([{":
            opened.append(CLOSING[char])
        elif char in ")]}" and (not opened or opened.pop() != char):
            return False
        index += 1

    return not opened


def volatile(message: str | None, address: str) -> str:
    message = ADDRESS.sub(lambda found: (found[1] or found[2]) + address, message or "")
    for pattern, replacement in VOLATILE:
        message = pattern.sub(replacement, message)

    return message


def set_items(lines: list[str], truncated: bool) -> list[str]:
    """The items listed after a header of a set, sorted, or left out when the explanation is cut
    short: which of them it keeps depends on their order"""
    out: list[str] = []
    index = 0
    while index < len(lines):
        out.append(lines[index])
        index += 1
        if not SET_HEADER.fullmatch(out[-1]):
            continue
        end = index
        while end < len(lines) and not AFTER_ITEMS.match(lines[end]):
            end += 1
        if not truncated:
            out.extend(sorted(lines[index:end]))
        index = end

    return out


class Brackets:
    """The text read bracket by bracket. A bracket that never closes, or closes another one, is a
    character like any other"""

    # Deeper brackets are left as they are, so a long nesting cannot exhaust the stack
    DEPTH = 50

    def __init__(self, text: str, *, collapse: bool = False) -> None:
        self.text = text
        # A set cut short, whose items the order of the hashes chose, and a repr unittest cut short
        # are left out
        self.collapse = collapse
        # Where a string or a pair of brackets starts => where it ends
        self.spans: dict[int, int] = {}
        opened: list[int] = []
        index = 0
        while index < len(text):
            char = text[index]
            if char in "'\"" and starts_string(text, index):
                self.spans[index] = string_end(text, index)
                index = self.spans[index]
                continue
            # A comparison such as a < b is not a bracket, a repr such as <Mock> is
            if char in CLOSING and (char != "<" or text[index + 1 : index + 2].isalpha()):
                opened.append(index)
            elif opened and char == CLOSING[text[opened[-1]]]:
                self.spans[opened.pop()] = index + 1
            index += 1

    def sorted(self) -> str:
        return self.part(0, len(self.text), 0)

    def part(self, start: int, end: int, depth: int) -> str:
        text = self.text
        out = []
        index = start
        while index < end:
            stop = self.spans.get(index)
            if stop is None:
                out.append(text[index])
                index += 1
                continue
            if self.collapse and self.cut(index, stop):
                out.append(text[index] + "..." + text[stop - 1])
            elif text[index] == "{" and depth < self.DEPTH:
                out.append("{" + self.set_items(index + 1, stop - 1, depth + 1) + "}")
            elif text[index] in CLOSING and depth < self.DEPTH:
                out.append(text[index] + self.part(index + 1, stop - 1, depth + 1) + text[stop - 1])
            else:
                out.append(text[index:stop])
            index = stop

        return "".join(out)

    def cut(self, start: int, stop: int) -> bool:
        """A set cut short anywhere, inside a repr of an item too, as saferepr cuts at a length.
        A dict keeps the order its keys were put in, cut short or not"""
        if CHARS.search(self.text, start, stop):
            return True
        if self.text[start] != "{" or "..." not in self.text[start:stop]:
            return False

        index = start + 1
        while index < stop - 1:
            if index in self.spans:
                index = self.spans[index]
                continue
            if self.text[index] == ":":
                return False
            index += 1

        return True

    def set_items(self, start: int, end: int, depth: int) -> str:
        """The items between braces, sorted when they are of a set. A dict has a colon after a key"""
        bounds = [start]
        keyed = False
        index = start
        while index < end:
            if index in self.spans:
                index = self.spans[index]
                continue
            if self.text.startswith(", ", index):
                bounds.append(index + 2)
            keyed = keyed or self.text[index] == ":"
            index += 1

        bounds.append(end + 2)
        items = [self.part(bounds[i], bounds[i + 1] - 2, depth) for i in range(len(bounds) - 1)]

        return ", ".join(items if keyed else sorted(items))


def starts_string(text: str, index: int) -> bool:
    """A quote after a word is an apostrophe, as in user's, unless the word is the prefix of a
    string, as in b'data'"""
    before = text[index - 1 : index]
    if before in tuple("bBrRuUfF"):
        before = text[index - 2 : index - 1]

    return not (before.isalnum() or before == "_")


def string_end(text: str, start: int) -> int:
    """Where a string that a repr shows ends: at its closing quote, or at the end of its line"""
    quote = text[start]
    index = start + 1
    while index < len(text) and text[index] not in (quote, "\n"):
        index += 2 if text[index] == "\\" else 1

    return min(index + 1, len(text)) if index < len(text) and text[index] == quote else index


def sorted_lines(block: str) -> str:
    end = "\n" if block.endswith("\n") else ""

    return "\n".join(sorted(block.rstrip("\n").split("\n"))) + end


@dataclass(frozen=True)
class Culprits:
    """Tests that make another test fail when they run before it, and a command that shows the failure.

    ids is empty when bisect could not reproduce the failure, or when the test failed with that seed
    with no other test before it (alone). names: readable test names, the ids by default.
    group_wide: culprits, of ids or of others, that other tests of their group stand in for, as when
    the state comes from a fixture or hook of the group and not from the test itself. others: further
    sets of tests, each of which breaks the test without the culprits as well. reproduced: no
    culprits, though the failure came back with the same order. broken: every run broke before it
    reached the test
    """

    ids: list[str]
    command: str
    names: list[str] | None = None
    alone: bool = False
    group_wide: list[str] = field(default_factory=list)
    others: list[list[str]] = field(default_factory=list)
    other_names: list[list[str]] | None = None
    reproduced: bool = False
    broken: bool = False

    def __post_init__(self) -> None:
        if self.names is None:
            object.__setattr__(self, "names", list(self.ids))

        if self.other_names is None:
            object.__setattr__(self, "other_names", [list(ids) for ids in self.others])

    @property
    def found(self) -> bool:
        return bool(self.ids)


@dataclass(frozen=True)
class EnvironmentCause:
    culprit: str
    key: str
    seed: int
    baseline: list[str]
    repair: list[str]
    reapply: list[str]
    detector: str = "environment"

    @property
    def label(self) -> str:
        """How the leaks of the culprit name this state"""
        if self.detector == "environment":
            return f"os.environ[{self.key!r}]"

        # Patches of one target are told apart by a number their leaks do not have
        return re.sub(r" #\d+$", "", self.key) if self.detector == "patch" else self.key

    @property
    def confirmed(self) -> bool:
        return (
            self.baseline == ["same_failure"] * 2
            and self.repair == ["passed"] * 2
            and self.reapply == ["same_failure"] * 2
        )


@dataclass(frozen=True)
class CulpritLeak:
    """A leak of a culprit, and whether an experiment tried it as the cause of the failure"""

    culprit: str
    message: str
    checked: bool
    confirmed: bool


@dataclass(frozen=True)
class Diagnosis:
    """Everything deflake found out about one test.

    isolated_runs and culprits are None when there was nothing to investigate, and isolated_runs
    also when the failure could not run alone: errors outside of tests, or a test whose runs alone
    all broke before it started
    """

    name: str
    id: str
    file_runs: Summary
    isolated_runs: Summary | None = None
    culprits: Culprits | None = None
    causes: list[EnvironmentCause] = field(default_factory=list)
    culprit_leaks: list[CulpritLeak] = field(default_factory=list)

    @property
    def verdict(self) -> Verdict:
        """random: it passed every time alone, and no tests before it were found to make it fail.
        failed: it could not be run alone, or every run of the bisect broke before it"""
        if self.file_runs.failures == 0:
            return "stable"
        if self.isolated_runs is None or self.isolated_runs.total == 0:
            return "failed"
        if self.fails_alone:
            return "fails_on_its_own"

        culprits = self.culprits
        if culprits is None or culprits.found:
            return "order_dependent"
        if culprits.broken:
            return "failed"

        # Even a failure that came back twice with the order: a test failing at random does so too
        return "random"

    @property
    def fails_alone(self) -> bool:
        """It failed in a run alone, or with no test before it in a bisect"""
        alone = self.isolated_runs is not None and self.isolated_runs.failures > 0

        return alone or (self.culprits is not None and self.culprits.alone)

    @property
    def clean(self) -> bool:
        """Nothing failed and nothing leaked"""
        return self.verdict == "stable" and not self.file_runs.leaks
