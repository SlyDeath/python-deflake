"""Integration with the real pytest-timeout signal and thread implementations."""

import json
import os
import subprocess
import sys

import pytest


@pytest.mark.parametrize("method", ["signal", "thread"])
@pytest.mark.parametrize("func_only", [False, True])
def test_timeout_service_state_is_not_a_shared_fixture_leak(project, deflake, method, func_only):
    root = project("pytest_regressions")
    with (root / "pytest.ini").open("a") as file:
        file.write(f"\ntimeout = 5\ntimeout_method = {method}\ntimeout_func_only = {func_only}\n")

    (root / "test_timeout_service.py").write_text(
        'import pytest\n\n@pytest.fixture(scope="module")\ndef shared():\n    yield\n\n'
        "def test_a(shared):\n    pass\n\ndef test_b(shared):\n    pass\n"
    )
    result = deflake("test_timeout_service.py", "-n", "1", "-f", "json", cwd=root)
    assert result.code == 0, result.out + result.err
    assert all(not row["leaks"] and not row["errors"] for row in result.json)


@pytest.mark.parametrize("method", ["signal", "thread"])
@pytest.mark.parametrize("output_format", ["text", "json"])
def test_timeout_trigger_is_reported_without_causal_confirmation(project, deflake, method, output_format):
    root = project("pytest_regressions")
    (root / "test_plugin_timeout.py").write_text(
        "import time\nimport pytest\n\n"
        f"@pytest.mark.timeout(0.1, method={method!r})\n"
        "def test_hangs():\n    time.sleep(5)\n"
    )
    result = deflake("test_plugin_timeout.py", "-n", "1", "-f", output_format, cwd=root)
    assert result.code == 1, result.out + result.err
    if output_format == "text":
        assert "test_hangs: failed 1 of 1 runs" in result.out, result.out
        assert "timeout" in result.out.lower()
        assert "cause:" not in result.out
        return

    victim = next(row for row in result.json if row["name"] == "test_hangs")
    assert victim["runs"]["failures"] == 1
    assert any("timeout" in error["message"].lower() for error in victim["errors"])
    assert not victim["causes"]


@pytest.mark.parametrize("method", ["signal", "thread"])
def test_timeout_causes_are_confirmed_only_for_completed_processes(project, method):
    root = project("pytest_regressions")
    (root / "test_timeout_cause.py").write_text(
        "import os\nimport time\nimport pytest\n\n"
        'def test_a_pollutes():\n    os.environ["TIMEOUT_CAUSE"] = "1"\n\n'
        f"@pytest.mark.timeout(0.5, method={method!r}, func_only=True)\n"
        "def test_b_victim():\n"
        '    if os.getenv("TIMEOUT_CAUSE"):\n        time.sleep(5)\n'
    )
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import random; random.sample=lambda population,k:list(range(k)); "
            "from deflake.cli import main; raise SystemExit(main())",
            "test_timeout_cause.py",
            "-n",
            "1",
            "-f",
            "json",
        ],
        cwd=root,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    rows = json.loads(result.stdout)
    victim = next(row for row in rows if row["name"] == "test_b_victim")
    causes = [cause for cause in victim["causes"] if cause["key"] == "TIMEOUT_CAUSE"]
    if method == "signal":
        assert causes, rows
        assert any(cause["confirmed"] for cause in causes), causes
        assert causes[0]["repair"] == ["passed"] * 2
    else:
        assert all(not cause["confirmed"] for cause in causes), causes
        assert not victim["causes"], victim
        assert victim["culprits"]["ids"] == ["test_timeout_cause.py::test_a_pollutes"]


@pytest.mark.parametrize("method", ["signal", "thread"])
@pytest.mark.parametrize("session_timeout", [False, True])
def test_session_timeout_cannot_hide_behind_a_failed_culprit(project, method, session_timeout):
    root = project("pytest_regressions")
    with (root / "pytest.ini").open("a") as file:
        file.write(f"\nsession_timeout = {int(session_timeout)}\ntimeout = 10\ntimeout_method = {method}\n")

    with (root / "conftest.py").open("a") as file:
        file.write(
            "\nimport pytest\nimport pytest_timeout\nfrom types import SimpleNamespace\n"
            "\n@pytest.hookimpl(tryfirst=True)\ndef pytest_configure(config):\n"
            "    pytest_timeout.time = SimpleNamespace(time=lambda: 0)\n"
        )

    (root / "test_session_cause.py").write_text(
        "import os\nimport pytest_timeout\n\n"
        "def test_a_pollutes():\n"
        '    os.environ["SESSION_CAUSE"] = "leaked"\n'
        '    assert False, "independent culprit failure"\n\n'
        "def test_b_victim():\n"
        "    pytest_timeout.time.time = lambda: 2\n"
        '    assert os.getenv("SESSION_CAUSE") is None, "polluted victim"\n'
    )
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import random; random.sample=lambda population,k:list(range(k)); "
            "from deflake.cli import main; raise SystemExit(main())",
            "test_session_cause.py",
            "-n",
            "1",
            "-f",
            "json",
        ],
        cwd=root,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    rows = json.loads(result.stdout)
    victim = next(row for row in rows if row["name"] == "test_b_victim")
    causes = [cause for cause in victim["causes"] if cause["key"] == "SESSION_CAUSE"]
    assert causes, rows
    if session_timeout:
        assert all(not cause["confirmed"] for cause in causes), causes
        assert all(cause["baseline"] == ["interrupted"] * 2 for cause in causes)
        assert any("session-timeout" in error["message"] for row in rows for error in row["errors"]), rows
    else:
        assert any(cause["confirmed"] for cause in causes), causes
        assert causes[0]["repair"] == ["passed"] * 2


def test_a_handler_set_before_the_signal_timer_is_not_a_leak_of_the_test(project, deflake):
    root = project("pytest_regressions")
    with (root / "pytest.ini").open("a") as file:
        file.write("\ntimeout = 30\ntimeout_method = signal\n")

    with (root / "conftest.py").open("a") as file:
        file.write("\nimport signal\n\nsignal.signal(signal.SIGALRM, lambda *_: None)\n")

    # A session fixture is torn down after the timer of the last test stops, or a test with no timer
    (root / "test_alarm.py").write_text(
        'import pytest\n\n\n@pytest.fixture(scope="session")\ndef service():\n    yield\n\n\n'
        "def test_a(service):\n    pass\n\n\n"
        "@pytest.mark.timeout(0)\ndef test_b(service):\n    pass\n"
    )
    result = deflake("test_alarm.py", "-n", "6", "-f", "json", cwd=root)
    assert result.code == 0, result.out + result.err
    assert [row["name"] for row in result.json] == ["test_a", "test_b"], result.out
    assert all(not row["leaks"] for row in result.json), result.out


def test_a_handler_a_test_leaves_without_a_timer_is_still_a_leak(project, deflake):
    root = project("pytest_regressions")
    (root / "test_alarm.py").write_text(
        "import signal\n\ndef test_sets():\n    signal.signal(signal.SIGALRM, lambda *_: None)\n"
    )
    result = deflake("test_alarm.py", "-n", "1", "-f", "json", cwd=root)
    test = next(row for row in result.json if row["name"] == "test_sets")
    assert test["leaks"] == [{"message": "the handler of SIGALRM was changed", "runs": 1}], result.out
