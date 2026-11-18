import os
from pathlib import Path

from deflake import events, result_file


def test_events_are_read_back_one_per_line(tmp_path: Path) -> None:
    path = tmp_path / "results.jsonl"
    events.start(path)
    events.append(path, started="t.py::test_a", name="test_a")
    events.append(path, id="t.py::test_a", passed=False, error="AssertionError: é\nnext", leaks=[])
    assert result_file.read(path) == [
        {"started": "t.py::test_a", "name": "test_a"},
        {"id": "t.py::test_a", "passed": False, "error": "AssertionError: é\nnext", "leaks": []},
    ]


def test_a_broken_last_line_is_skipped(tmp_path: Path) -> None:
    path = tmp_path / "results.jsonl"
    events.append(path, started="a")
    with path.open("a") as file:
        file.write('{"id": "a", "pas')

    assert result_file.read(path) == [{"started": "a"}]


def test_lone_surrogates_do_not_break_the_file(tmp_path: Path) -> None:
    path = tmp_path / "results.jsonl"
    events.append(path, error=b"caf\xe9".decode("utf-8", "surrogateescape"))
    assert result_file.read(path) == [{"error": "caf?"}]


def test_ids_go_through_a_file(tmp_path: Path) -> None:
    path = tmp_path / "order"
    events.write_ids(path, ["t.py::test_b[x y]", "t.py::test_a"])
    assert events.read_ids(path) == ["t.py::test_b[x y]", "t.py::test_a"]


def test_process_birth_identity_is_stable_for_a_live_process():
    identity = events.process_identity(os.getpid())
    assert identity is not None
    assert identity == events.process_identity(os.getpid())


def test_linux_birth_identity_handles_parentheses_in_process_names(monkeypatch):
    state = "123 (worker (service)) " + " ".join(["S", *(["0"] * 18), "98765", "0"])
    monkeypatch.setattr(events.sys, "platform", "linux")
    monkeypatch.setattr(Path, "read_bytes", lambda self: state.encode())
    assert events.process_identity(123) == "98765"


def test_non_utf8_linux_comm_does_not_hide_birth_identity(tmp_path, monkeypatch):
    stat = tmp_path / "stat"
    stat.write_bytes(b"123 (x\xff) " + b"S " + b"0 " * 18 + b"98765 0")
    monkeypatch.setattr(events.sys, "platform", "linux")
    monkeypatch.setattr(events, "Path", lambda path: stat)
    assert events.process_identity(123) == "98765"
