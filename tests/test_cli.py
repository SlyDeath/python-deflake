import io
import json
import signal
import threading
from pathlib import Path

import pytest

from deflake import cli


def test_a_count_that_is_not_a_number_is_a_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["test_x.py", "-n", "many"], io.StringIO(), io.StringIO()) == 2
    # argparse writes its usage errors to stderr itself
    assert "many is not a whole number" in capsys.readouterr().err


def test_a_test_name_after_a_folder_is_an_error(tmp_path: Path) -> None:
    err = io.StringIO()
    assert cli.main([f"{tmp_path}::test_a"], io.StringIO(), err) == 2
    assert err.getvalue() == f"deflake: {tmp_path} is a folder: a line or a test name needs a test file\n"


def test_an_unexpected_error_asks_for_a_report(monkeypatch: pytest.MonkeyPatch) -> None:
    def run(*args: object) -> int:
        raise RuntimeError("broken")

    monkeypatch.setattr(cli, "run", run)
    err = io.StringIO()
    assert cli.main(["test_x.py"], io.StringIO(), err) == 2
    assert err.getvalue().startswith("deflake: unexpected error, please report it: RuntimeError: broken\n")


def test_a_stream_without_a_file_is_not_the_same_file() -> None:
    assert not cli.same_file(io.StringIO(), io.StringIO())


def test_signals_are_left_alone_off_the_main_thread() -> None:
    handlers = []

    def watch() -> None:
        with cli.terminated_as_interrupt():
            handlers.append(signal.getsignal(signal.SIGTERM))

    before = signal.getsignal(signal.SIGTERM)
    thread = threading.Thread(target=watch)
    thread.start()
    thread.join()
    assert handlers == [before]


def test_a_folder_without_test_files_is_an_error(tmp_path: Path) -> None:
    (tmp_path / "helpers.py").write_text("")
    err = io.StringIO()
    assert cli.main([str(tmp_path)], io.StringIO(), err) == 2
    assert err.getvalue().startswith("deflake: no test files (")


def test_a_file_of_a_folder_that_cannot_run_is_reported_not_checked(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "test_a.py").write_text("def test_a():\n    pass\n")
    out, err = io.StringIO(), io.StringIO()
    # The django runner needs manage.py, which this folder does not have
    assert cli.main([".", "-r", "django", "-f", "json"], out, err) == 2
    assert "deflake: the django runner needs manage.py in the current folder\n" in err.getvalue()
    (row,) = json.loads(out.getvalue())
    assert row["file"].endswith("test_a.py")
    assert row["verdict"] == "not_checked"
    assert "needs manage.py" in row["errors"][0]["message"]
