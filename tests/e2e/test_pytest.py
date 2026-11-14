"""The deflake command on pytest projects"""

import contextlib
import os
import re
import shlex
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from tests.e2e.conftest import Deflake

Project = Callable[[str], Path]


def test_one_replacement_does_not_imply_a_shared_fixture(project: Project, deflake: Deflake) -> None:
    result = deflake("test_two_polluters.py", "-n", "1", "-f", "json", cwd=project("pytest_regressions"))
    assert result.code == 1, result.out + result.err
    victim = next(test for test in result.json if test["name"] == "TestVictim::test_victim")
    assert len(victim["culprits"]["ids"]) == 1
    assert victim["culprits"]["group_wide"] == []
    assert "fixture" not in victim["culprits"]["names"][0]


def test_bisect_drops_a_culprit_after_its_cleanup_was_removed(project: Project, deflake: Deflake) -> None:
    result = deflake("test_cleanup.py", "-n", "1", "-f", "json", cwd=project("pytest_regressions"))
    assert result.code == 1, result.out + result.err
    victim = next(test for test in result.json if test["name"] == "test_e_victim")
    assert victim["verdict"] == "order_dependent"
    assert victim["culprits"]["ids"] == ["test_cleanup.py::test_c_pollutes"]


def test_a_parameter_id_with_separators_still_identifies_the_class_fixture(
    project: Project,
    deflake: Deflake,
) -> None:
    result = deflake("test_groups.py", "-n", "1", "-f", "json", cwd=project("pytest_regressions"))
    assert result.code == 1, result.out + result.err
    victim = next(test for test in result.json if test["name"] == "TestVictim::test_victim")
    assert victim["verdict"] == "order_dependent"
    assert len(victim["culprits"]["ids"]) == 1
    assert victim["culprits"]["group_wide"] == victim["culprits"]["ids"]
    assert "any other test of its group" in victim["culprits"]["names"][0]


# The victim fails in half of the orders, so 20 runs miss it once in a million
def test_finds_the_culprit_of_an_order_dependent_test(project: Project, deflake: Deflake) -> None:
    result = deflake("test_order.py", "-n", "20", cwd=project("pytest"))
    assert result.code == 1, result.err
    victim = result.out.split("test_victim: ")[1]
    assert "verdict: fails only after other tests (alone: failed 0 of 20 runs (0%))" in victim
    assert "culprit: test_pollutes\n" in victim
    assert "reproduce: DEFLAKE_SEED=" in victim
    assert "python -m pytest -p deflake.pytest_plugin test_order.py" in victim
    assert "assert not True" in victim
    # The dict changed in place is no state an experiment can put back; the variable was tried
    assert "  test_pollutes leaks: test_order.STATE was changed (not checked as the cause)\n" in victim
    assert "DEFLAKE_FIXTURE_FLAG was set (not checked" not in victim
    assert (
        "environment cause: DEFLAKE_FIXTURE_FLAG from test_order.py::test_pollutes: not confirmed" in victim
    )


def test_reports_leaks(project: Project, deflake: Deflake) -> None:
    result = deflake("test_order.py", "-n", "3", cwd=project("pytest"))
    assert "test_pollutes: failed 0 of 3 runs (0%)\n" in result.out
    assert "  leaks: os.environ['DEFLAKE_FIXTURE_FLAG'] was set (3 of 3 runs)\n" in result.out


def test_reproduce_command_shows_the_failure(project: Project, deflake: Deflake) -> None:
    import os
    import shlex
    import subprocess
    import sys

    root = project("pytest")
    result = deflake("test_order.py", "-n", "20", "-f", "json", cwd=root)
    victim = next(test for test in result.json if test["name"] == "test_victim")
    variable, *command = shlex.split(victim["culprits"]["command"])
    name, value = variable.split("=")
    command[0] = sys.executable
    run = subprocess.run(command, cwd=root, env={**os.environ, name: value}, capture_output=True, text=True)
    assert run.returncode == 1
    assert "FAILED test_order.py::test_victim" in run.stdout


def test_a_test_that_fails_alone_too(project: Project, deflake: Deflake) -> None:
    result = deflake("test_cases.py::test_always_fails", "-n", "2", cwd=project("pytest_cases"))
    assert result.code == 1
    assert result.out.startswith("test_always_fails: failed 2 of 2 runs (100%)\n")
    assert "  verdict: fails on its own (alone: failed 2 of 2 runs (100%))\n" in result.out
    assert "assert 1 == 2" in result.out


def test_a_test_that_takes_its_process_down(project: Project, deflake: Deflake) -> None:
    result = deflake("test_crash.py", "-n", "2", cwd=project("pytest_cases"))
    assert result.code == 1
    assert "test_crashes: failed 2 of 2 runs (100%)" in result.out
    assert "the test process exited with status 3 during this test" in result.out


def test_a_skipped_test_is_not_reported(project: Project, deflake: Deflake) -> None:
    result = deflake("test_cases.py::test_skipped", "-n", "2", cwd=project("pytest_cases"))
    assert result.code == 0
    assert result.out == ""
    assert "the tests at test_skipped skipped themselves in every run, nothing to report" in result.err


def test_a_line_selects_the_test_around_it(project: Project, deflake: Deflake) -> None:
    result = deflake("test_cases.py:19", "-n", "2", cwd=project("pytest_cases"))
    assert result.code == 0, result.err
    assert result.out == "test_params[1]: failed 0 of 2 runs (0%)\ntest_params[2]: failed 0 of 2 runs (0%)\n"


def test_a_name_without_a_test_is_an_error(project: Project, deflake: Deflake) -> None:
    result = deflake("test_cases.py::test_nope", "-n", "2", cwd=project("pytest_cases"))
    assert result.code == 2
    assert "deflake: no test named test_nope in test_cases.py" in result.err


def test_timeout_stops_a_hanging_test(project: Project, deflake: Deflake) -> None:
    result = deflake("test_hang.py", "-n", "1", "-t", "10", cwd=project("pytest_cases"))
    assert result.code == 1
    assert "the test process timed out after 10 seconds during this test" in result.out


def test_a_file_that_cannot_be_collected(project: Project, deflake: Deflake) -> None:
    result = deflake("test_broken.py", "-n", "2", cwd=project("pytest_broken"))
    assert result.code == 1
    assert "(outside of tests): failed 2 of 2 runs (100%)" in result.out.replace(
        "errors outside of tests",
        "(outside of tests)",
    )
    assert "error collecting test_broken.py" in result.out


def test_json_report(project: Project, deflake: Deflake) -> None:
    result = deflake("test_order.py", "-n", "4", "-f", "json", cwd=project("pytest"))
    tests = {test["name"]: test for test in result.json}
    assert tests["test_unrelated"] == {
        "file": "test_order.py",
        "name": "test_unrelated",
        "id": "test_order.py::test_unrelated",
        "verdict": "stable",
        "runs": {"total": 4, "failures": 0, "failure_rate": 0.0, "skipped": 0},
        "isolated_runs": None,
        "errors": [],
        "leaks": [],
        "culprits": None,
        "marked_flaky": False,
        "causes": [],
        "culprit_leaks": [],
    }
    assert tests["test_pollutes"]["leaks"] == [
        {"message": "os.environ['DEFLAKE_FIXTURE_FLAG'] was set", "runs": 4},
        {"message": "test_order.STATE was changed", "runs": 4},
    ]


def test_a_folder_checks_every_test_file(project: Project, deflake: Deflake) -> None:
    root = project("pytest")
    (root / "sub").mkdir()
    (root / "sub" / "test_more.py").write_text("def test_fine():\n    pass\n")
    (root / ".venv" / "lib").mkdir(parents=True)
    (root / ".venv" / "lib" / "test_installed.py").write_text("def test_other():\n    pass\n")
    result = deflake(".", "-n", "2", cwd=root)
    assert "== sub/test_more.py (1 of 2)\ntest_fine: failed 0 of 2 runs (0%)\n" in result.out
    assert "== test_order.py (2 of 2)" in result.out
    assert "deflake: checked 2 files" in result.err
    assert "failing or leaking tests in:\n  test_order.py" in result.err


@pytest.mark.parametrize(
    ("messages", "format"),
    [(subprocess.DEVNULL, "text"), (subprocess.STDOUT, "text"), (subprocess.STDOUT, "json")],
    ids=["stdout", "both", "json"],
)
def test_a_folder_read_only_in_part_is_not_a_success(project: Project, messages: int, format: str) -> None:
    root = project("pytest")
    (root / "test_more.py").write_text("def test_fine():\n    pass\n")
    process = subprocess.Popen(
        [sys.executable, "-m", "deflake", ".", "-n", "1", "-f", format],
        cwd=root,
        stdout=subprocess.PIPE,
        stderr=messages,
    )
    assert process.stdout is not None
    process.stdout.readline()
    process.stdout.close()
    process.wait(timeout=240)
    assert process.returncode == 141


def test_plugins_that_reorder_or_retry_are_switched_off(project: Project, deflake: Deflake) -> None:
    root = project("pytest")
    (root / "pytest.ini").write_text("[pytest]\naddopts = --reruns 5 -p randomly -x\n")
    result = deflake("test_order.py", "-n", "20", cwd=root)
    assert "culprit: test_pollutes" in result.out, result.out + result.err


def test_usage_errors(project: Project, deflake: Deflake) -> None:
    root = project("pytest")
    assert deflake(cwd=root).code == 2
    assert deflake("test_order.py", "-n", "0", cwd=root).code == 2
    missing = deflake("nope.py", cwd=root)
    assert (missing.code, missing.err) == (2, "deflake: no such file: nope.py\n")


def test_jobs_run_at_once_and_find_the_same(project: Project, deflake: Deflake) -> None:
    root = project("pytest")
    (root / "conftest.py").write_text(
        "import os\n\n\ndef pytest_sessionstart(session):\n"
        '    with open(f"jobs-{os.getpid()}", "w") as file:\n'
        '        file.write(os.environ.get("DEFLAKE_JOB", ""))\n'
    )
    result = deflake("test_order.py", "-n", "20", "-j", "4", cwd=root)
    assert "culprit: test_pollutes" in result.out, result.out + result.err
    # Bisect runs one process at a time, without a job number
    jobs = {path.read_text() for path in root.glob("jobs-*")} - {""}
    assert jobs <= {"1", "2", "3", "4"}
    assert len(jobs) > 1


@pytest.mark.parametrize(
    ("number", "code", "message"),
    [
        (signal.SIGINT, 130, "deflake: interrupted"),
        (signal.SIGTERM, 128 + signal.SIGTERM, "deflake: terminated"),
        (signal.SIGHUP, 128 + signal.SIGHUP, "deflake: terminated"),
    ],
)
def test_interrupt_stops_the_test_processes(project: Project, number: int, code: int, message: str) -> None:

    root = project("pytest_cases")
    (root / "test_hang.py").write_text(
        "import os\nimport time\n\n\ndef test_hangs():\n"
        '    open(f"pid-{os.getpid()}", "w").close()\n    time.sleep(60)\n'
    )
    process = subprocess.Popen(
        [sys.executable, "-m", "deflake", "test_hang.py", "-n", "2", "-j", "2"],
        cwd=root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.monotonic() + 30
    while len(list(root.glob("pid-*"))) < 2 and time.monotonic() < deadline:
        time.sleep(0.1)

    process.send_signal(number)
    _, err = process.communicate(timeout=30)
    assert process.returncode == code
    assert message in err
    time.sleep(0.5)
    for path in root.glob("pid-*"):
        pid = int(path.name.removeprefix("pid-"))
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            continue
        raise AssertionError(f"test process {pid} is still running")


def test_an_interrupt_lets_the_tests_tear_down(project: Project) -> None:
    root = project("pytest_cases")
    (root / "test_set_up.py").write_text(
        "import os\nimport time\nfrom pathlib import Path\n\nimport pytest\n\n\n"
        '@pytest.fixture(scope="session")\ndef service():\n'
        '    marker = Path("service")\n    marker.touch()\n    yield\n    marker.unlink()\n\n\n'
        "def test_uses_the_service(service):\n"
        '    Path("pid").write_text(str(os.getpid()))\n    time.sleep(60)\n'
    )
    process = subprocess.Popen(
        [sys.executable, "-m", "deflake", "test_set_up.py", "-n", "1"],
        cwd=root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 30
        while not (root / "service").exists() and time.monotonic() < deadline:
            time.sleep(0.1)

        assert (root / "service").exists(), "the test never started"
        process.send_signal(signal.SIGINT)
        _, err = process.communicate(timeout=30)
    finally:
        process.kill()
        # The test process is in a group of its own: deflake killed early leaves it running
        with contextlib.suppress(OSError, ValueError):
            os.kill(int((root / "pid").read_text()), signal.SIGKILL)

    assert process.returncode == 130, err
    assert not (root / "service").exists()


def test_a_terminal_closed_while_the_tests_tear_down_kills_them(project: Project) -> None:
    root = project("pytest_cases")
    (root / "test_stubborn.py").write_text(
        "import os\nimport signal\nimport time\nfrom pathlib import Path\n\n\n"
        "def test_ignores_interrupts():\n    signal.signal(signal.SIGINT, signal.SIG_IGN)\n"
        '    Path("pid").write_text(str(os.getpid()))\n    time.sleep(60)\n'
    )
    process = subprocess.Popen(
        [sys.executable, "-m", "deflake", "test_stubborn.py", "-n", "1"],
        cwd=root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 30
        while not (root / "pid").exists() and time.monotonic() < deadline:
            time.sleep(0.1)

        assert (root / "pid").exists(), "the test never started"
        process.send_signal(signal.SIGINT)
        time.sleep(0.5)
        process.send_signal(signal.SIGHUP)
        _, err = process.communicate(timeout=3)
        # The code of the signal that ended the wait, as deflake did not finish stopping on the first one
        assert process.returncode in {130, 128 + signal.SIGHUP}, err

        pid = int((root / "pid").read_text())
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        else:
            raise AssertionError(f"test process {pid} is still running")
    finally:
        process.kill()
        with contextlib.suppress(OSError, ValueError):
            os.kill(int((root / "pid").read_text()), signal.SIGKILL)


def test_a_test_marked_flaky(project: Project, deflake: Deflake) -> None:
    root = project("pytest")
    (root / "test_marked.py").write_text(
        "import pytest\n\n\n@pytest.mark.flaky(reruns=3)\ndef test_marked():\n    pass\n"
    )
    result = deflake("test_marked.py", "-n", "2", cwd=root)
    assert result.out == (
        "test_marked: failed 0 of 2 runs (0%)\n"
        "  marked flaky, but did not fail here: the marker may be out of date\n"
    ), result.out + result.err


def test_markers_of_the_plugins_deflake_turns_off_pass_strict_markers(
    project: Project,
    deflake: Deflake,
) -> None:
    root = project("pytest_cases")
    (root / "pytest.ini").write_text("[pytest]\naddopts = --strict-markers -n 2\n")
    (root / "test_strict.py").write_text(
        "import pytest\n\n\n"
        '@pytest.mark.xdist_group("db")\n@pytest.mark.random_order(disabled=True)\n'
        "@pytest.mark.flaky(reruns=1)\ndef test_marked():\n    pass\n"
    )
    result = deflake("test_strict.py", "-n", "1", "-f", "json", cwd=root)
    assert [test["name"] for test in result.json] == ["test_marked"], result.out + result.err
    assert result.json[0]["runs"]["failures"] == 0, result.out


def test_a_pytest_toml_makes_a_pytest_project(project: Project, deflake: Deflake) -> None:
    root = project("unittest")
    (root / "pytest.toml").write_text("[pytest]\n")
    result = deflake("test_order.py::OtherTest", "-n", "1", "-f", "json", cwd=root)
    assert [test["id"] for test in result.json] == ["test_order.py::OtherTest::test_one"], result.err


def test_a_line_does_not_select_a_test_of_a_mixin(project: Project, deflake: Deflake) -> None:
    root = project("pytest_cases")
    (root / "mixin.py").write_text("class Mixin:\n    def test_mixed(self):\n        pass\n")
    (root / "test_mixin.py").write_text(
        "from mixin import Mixin\n\n\nclass TestMixed(Mixin):\n    def test_own(self):\n        pass\n"
    )
    # Line 2 holds the test of the mixin in mixin.py, and nothing but the class in test_mixin.py
    result = deflake("test_mixin.py:5", "-n", "1", "-f", "json", cwd=root, env={"PYTHONPATH": str(root)})
    assert [test["name"] for test in result.json] == ["TestMixed::test_own"], result.err
    line_two = deflake("test_mixin.py:2", "-n", "1", "-f", "json", cwd=root, env={"PYTHONPATH": str(root)})
    assert line_two.code == 2
    assert "no test found at test_mixin.py:2" in line_two.err


def test_a_leak_said_twice_in_a_run_counts_once(project: Project, deflake: Deflake) -> None:
    root = project("pytest_cases")
    (root / "test_class_fixture.py").write_text(
        "import pytest\n\nSTATE = []\n\n\n@pytest.fixture(scope='class')\ndef grows():\n"
        "    yield\n    STATE.append(1)\n\n\n@pytest.mark.usefixtures('grows')\nclass TestOne:\n"
        "    def test_one(self):\n        pass\n\n\n@pytest.mark.usefixtures('grows')\nclass TestTwo:\n"
        "    def test_two(self):\n        pass\n"
    )
    result = deflake("test_class_fixture.py", "-n", "2", cwd=root)
    assert "test_class_fixture.STATE was changed (2 of 2 runs)" in result.out, result.out + result.err


def test_a_generator_of_fake_data_is_not_a_leak(project: Project, deflake: Deflake) -> None:
    root = project("pytest_cases")
    (root / "test_fake.py").write_text(
        "from faker import Faker\n\nfake = Faker()\n\n\ndef test_name():\n    assert fake.name()\n"
    )
    result = deflake("test_fake.py", "-n", "2", cwd=root)
    assert result.out == "test_name: failed 0 of 2 runs (0%)\n", result.out + result.err


def test_a_test_that_hangs_on_its_own_is_not_bisected(project: Project, deflake: Deflake) -> None:
    root = project("pytest_cases")
    (root / "test_hang_alone.py").write_text(
        "import time\n\n\ndef test_first():\n    pass\n\n\ndef test_hangs():\n    time.sleep(60)\n"
    )
    result = deflake("test_hang_alone.py", "-n", "1", "-t", "5", cwd=root)
    hangs = result.out.split("test_hangs: ")[1]
    assert "verdict: fails on its own" in hangs, result.out
    assert "culprit" not in hangs


def test_a_process_that_hangs_after_a_failed_test(project: Project, deflake: Deflake) -> None:
    root = project("pytest_cases")
    (root / "conftest.py").write_text(
        "import time\n\n\ndef pytest_unconfigure(config):\n    time.sleep(60)\n"
    )
    result = deflake("test_cases.py::test_always_fails", "-n", "1", "-t", "5", cwd=root)
    assert "errors outside of tests: failed 1 of 1 runs (100%)" in result.out, result.out
    assert "the test process timed out after 5 seconds after its tests" in result.out


def test_a_process_that_crashes_after_a_failed_test(project: Project, deflake: Deflake) -> None:
    root = project("pytest_cases")
    # SIGKILL, not abort(): macOS shows a crash report for every aborted process
    (root / "conftest.py").write_text(
        "import os\nimport signal\n\n\ndef pytest_unconfigure(config):\n"
        "    os.kill(os.getpid(), signal.SIGKILL)\n"
    )
    result = deflake("test_cases.py::test_always_fails", "-n", "1", cwd=root)
    assert "errors outside of tests: failed 1 of 1 runs (100%)" in result.out, result.out
    assert "the test process was killed by SIGKILL after its tests" in result.out


def test_a_line_does_not_select_a_class_of_another_module(project: Project, deflake: Deflake) -> None:
    root = project("pytest_cases")
    (root / "shared.py").write_text(
        "class TestShared:\n    def test_one(self):\n        pass\n\n    def test_two(self):\n        pass\n"
    )
    (root / "test_imported.py").write_text(
        "from shared import TestShared\n\n\ndef test_here():\n    pass\n\n\n\n"
    )
    env = {"PYTHONPATH": str(root)}
    result = deflake("test_imported.py:7", "-n", "1", "-f", "json", cwd=root, env=env)
    assert [test["name"] for test in result.json] == ["test_here"], result.err


def test_a_hangup_ignored_on_purpose_stays_ignored(project: Project) -> None:
    root = project("pytest_cases")
    (root / "test_slow.py").write_text(
        "import os\nimport time\n\n\ndef test_slow():\n"
        '    open(f"pid-{os.getpid()}", "w").close()\n    time.sleep(2)\n'
    )
    # As nohup starts it
    process = subprocess.Popen(
        [sys.executable, "-m", "deflake", "test_slow.py", "-n", "2"],
        cwd=root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        preexec_fn=lambda: signal.signal(signal.SIGHUP, signal.SIG_IGN),
    )
    deadline = time.monotonic() + 30
    while not list(root.glob("pid-*")) and time.monotonic() < deadline:
        time.sleep(0.1)

    process.send_signal(signal.SIGHUP)
    out, err = process.communicate(timeout=60)
    assert process.returncode == 0, err
    assert out == "test_slow: failed 0 of 2 runs (0%)\n"


def test_a_service_of_the_project_stops_as_without_deflake(project: Project, deflake: Deflake) -> None:
    root = project("pytest")
    # A service the project starts for its tests and stops with SIGTERM when they end
    (root / "conftest.py").write_text(
        "import subprocess\nimport sys\n\n"
        "service = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(600)'])\n\n\n"
        "def pytest_unconfigure(config):\n    service.terminate()\n    service.wait()\n"
    )
    result = deflake("test_order.py", "-n", "20", "-t", "60", cwd=root)
    assert "culprit: test_pollutes" in result.out, result.out + result.err
    assert "timed out" not in result.out


@pytest.mark.parametrize("detached", [False, True])
def test_processes_the_tests_leave_running_are_stopped(
    project: Project,
    deflake: Deflake,
    detached: bool,
) -> None:
    root = project("pytest_cases")
    (root / "test_background.py").write_text(
        "import os\nimport subprocess\nimport sys\n\n\ndef test_starts():\n"
        "    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(600)'],\n"
        f"                             start_new_session={detached}, env={{}})\n"
        f"    assert os.getsid(child.pid) == {'child.pid' if detached else 'os.getsid(0)'}\n"
        '    open(f"pid-{child.pid}", "w").close()\n'
    )
    deflake("test_background.py", "-n", "2", cwd=root)
    pids = [int(path.name.removeprefix("pid-")) for path in root.glob("pid-*")]
    assert len(pids) == 2
    time.sleep(0.5)
    alive = []
    for pid in pids:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            continue
        alive.append(pid)
        os.kill(pid, signal.SIGKILL)

    assert not alive, f"processes the test started are still running: {alive}"


def test_leaks_of_an_expected_failure_stay_its_own(project: Project, deflake: Deflake) -> None:
    root = project("pytest_cases")
    (root / "test_xfail_leak.py").write_text(
        "import os\n\nimport pytest\n\n\n@pytest.fixture(scope='module')\ndef shared():\n    yield\n\n\n"
        "@pytest.mark.xfail\ndef test_fails(shared):\n    os.environ['XFAIL_LEAK'] = '1'\n"
        "    assert False\n\n\n"
        "def test_clean(shared):\n    pass\n"
    )
    result = deflake("test_xfail_leak.py", "-n", "6", "-f", "json", cwd=root)
    leaks = {test["name"]: [leak["message"] for leak in test["leaks"]] for test in result.json}
    assert leaks.get("test_clean", []) == [], result.out


def test_a_leak_of_a_skipped_test_is_not_told_again_by_the_next(tmp_path: Path, deflake: Deflake) -> None:
    (tmp_path / "conftest.py").write_text(
        "import os\n\nimport pytest\n\n\n@pytest.fixture(autouse=True)\n"
        "def mode():\n    os.environ.setdefault('DEFLAKE_APP_MODE', 'test')\n"
    )
    (tmp_path / "test_skip.py").write_text(
        "import unittest\n\n\nclass Test(unittest.TestCase):\n"
        "    @unittest.skip('later')\n    def test_a(self):\n        pass\n\n"
        "    def test_b(self):\n        pass\n\n    def test_c(self):\n        pass\n"
    )
    result = deflake("test_skip.py", "-n", "8", cwd=tmp_path)
    counts = re.findall(r"DEFLAKE_APP_MODE'\] was set \((\d+) of 8 runs\)", result.out)
    assert sum(map(int, counts)) == 8, result.out + result.err


def test_a_closed_stderr_still_gives_the_report(project: Project) -> None:
    root = project("pytest")
    (root / "test_fine.py").write_text("def test_fine():\n    pass\n")
    command = f"{shlex.quote(sys.executable)} -m deflake test_fine.py -n 1 2>&-"
    run = subprocess.run(command, shell=True, cwd=root, stdout=subprocess.PIPE, text=True)
    assert (run.returncode, run.stdout) == (0, "test_fine: failed 0 of 1 runs (0%)\n")


@pytest.mark.parametrize("change", ["set", "change", "remove"])
def test_confirms_the_environment_change_that_causes_failure(
    project: Project,
    deflake: Deflake,
    change: str,
) -> None:
    root = project("pytest_regressions")
    path = root / "test_environment_cause.py"
    if change != "set":
        source = path.read_text().replace("import os", "import os\nos.environ['CAUSE_FLAG'] = 'original'")
        source = source.replace("is None", "== 'original'")
        if change == "remove":
            source = source.replace("os.environ['CAUSE_FLAG'] = 'leaked'", "del os.environ['CAUSE_FLAG']")
        path.write_text(source)

    result = deflake(path.name, "-n", "1", "-f", "json", cwd=root)
    assert result.code == 1, result.out + result.err
    victim = next(test for test in result.json if test["name"] == "test_b_victim")
    causes = victim.get("causes", [])
    assert any(cause["key"] == "CAUSE_FLAG" and cause["confirmed"] for cause in causes), causes
    assert not any(cause["key"] == "UNRELATED_FLAG" and cause["confirmed"] for cause in causes)


@pytest.mark.parametrize(
    "replacement",
    ["raise ValueError('different failure')", "pytest.skip('not a pass')"],
)
def test_environment_repair_must_pass(project: Project, deflake: Deflake, replacement: str) -> None:
    root = project("pytest_regressions")
    path = root / "test_environment_cause.py"
    path.write_text(
        path.read_text()
        .replace("import os", "import os\nimport pytest")
        .replace(
            "def test_b_victim():",
            "def test_b_victim():\n"
            "    if 'CAUSE_FLAG' not in os.environ and 'UNRELATED_FLAG' in os.environ:\n        "
            + replacement,
        )
    )
    result = deflake(path.name, "-n", "1", "-f", "json", cwd=root)
    victim = next(test for test in result.json if test["name"] == "test_b_victim")
    assert victim["causes"]
    assert not any(cause["confirmed"] for cause in victim["causes"])
    cause = next(cause for cause in victim["causes"] if cause["key"] == "CAUSE_FLAG")
    assert cause["repair"] == (["skipped"] * 2 if "skip" in replacement else ["different_failure"] * 2)


def test_environment_evidence_is_shown_in_text(project: Project, deflake: Deflake) -> None:
    result = deflake("test_environment_cause.py", "-n", "1", cwd=project("pytest_regressions"))
    assert result.code == 1
    assert (
        "environment cause: CAUSE_FLAG from test_environment_cause.py::test_a_pollutes: confirmed"
        in result.out
    )
    assert "repair: passed, passed; reapply: same_failure, same_failure" in result.out


def test_environment_baseline_must_reproduce_the_original_error(project: Project, deflake: Deflake) -> None:
    root = project("pytest_regressions")
    (root / "test_different.py").write_text(
        "import os\n\n"
        "def test_a_context():\n    os.environ['CONTEXT_FLAG'] = 'yes'\n\n"
        "def test_b_pollutes():\n    os.environ['CAUSE_FLAG'] = 'yes'\n\n"
        "def test_c_victim():\n"
        "    if os.getenv('CAUSE_FLAG'):\n"
        "        if os.getenv('CONTEXT_FLAG'):\n"
        "            raise ValueError('original failure')\n"
        "        raise TypeError('different failure')\n"
    )
    result = deflake("test_different.py", "-n", "1", "-f", "json", cwd=root)
    victim = next(test for test in result.json if test["name"] == "test_c_victim")
    # The original failure takes both tests: one alone breaks the victim another way
    assert victim["culprits"]["ids"] == [
        "test_different.py::test_a_context",
        "test_different.py::test_b_pollutes",
    ]
    causes = {cause["key"]: cause for cause in victim["causes"]}
    # Putting back the context gives the other failure, which is no repair
    assert causes["CONTEXT_FLAG"]["repair"] == ["different_failure"] * 2
    assert not causes["CONTEXT_FLAG"]["confirmed"]
    assert causes["CAUSE_FLAG"]["confirmed"]


@pytest.mark.parametrize("operation", ["change", "remove"])
def test_confirms_module_metadata_without_losing_other_globals(
    project: Project,
    deflake: Deflake,
    operation: str,
) -> None:
    root = project("pytest_regressions")
    if operation == "remove":
        path = root / "test_module_cause.py"
        path.write_text(
            path.read_text().replace("metadata_state.__file__ = None", "del metadata_state.__file__")
        )

    result = deflake("test_module_cause.py", "-n", "1", "-f", "json", cwd=root)
    assert result.code == 1, result.out + result.err
    victim = next(row for row in result.json if row["name"] == "test_b_victim")
    causes = victim.get("causes", [])
    assert any(cause["key"] == "metadata_state.__file__" and cause["confirmed"] for cause in causes), causes
    polluter = next(row for row in result.json if row["name"] == "test_a_pollutes")
    assert not any("metadata_state.FLAG was removed" in leak["message"] for leak in polluter["leaks"])
    change = "removed" if operation == "remove" else "changed"
    assert any(f"metadata_state.__file__ was {change}" in leak["message"] for leak in polluter["leaks"])
    assert not any(cause["key"] == "metadata_state.UNRELATED" and cause["confirmed"] for cause in causes)


@pytest.mark.parametrize("operation", ["set", "change", "remove"])
def test_confirms_scalar_module_changes(project: Project, deflake: Deflake, operation: str) -> None:
    root = project("pytest_regressions")
    attribute = "NEW_FLAG" if operation == "set" else "FLAG"
    mutation = (
        f"del metadata_state.{attribute}"
        if operation == "remove"
        else f"metadata_state.{attribute} = 'leaked'"
    )
    expected = "None" if operation == "set" else "'original'"
    (root / "test_scalar.py").write_text(
        "import metadata_state\n\n"
        f"def test_a_pollutes():\n    {mutation}\n\n"
        "def test_b_victim():\n"
        f"    assert getattr(metadata_state, '{attribute}', None) == {expected}\n"
    )
    result = deflake("test_scalar.py", "-n", "1", "-f", "json", cwd=root)
    victim = next(row for row in result.json if row["name"] == "test_b_victim")
    assert any(
        cause["detector"] == "module" and cause["key"] == f"metadata_state.{attribute}" and cause["confirmed"]
        for cause in victim["causes"]
    ), victim["causes"]


@pytest.mark.parametrize("runner", ["pytest", "unittest", "django"])
@pytest.mark.parametrize("ending", ["normal", "timeout", "interrupt"])
def test_detached_services_are_stopped_on_every_exit(project: Project, runner: str, ending: str) -> None:
    root = project("pytest_cases" if runner == "pytest" else runner)
    path = "shop/test_detached.py" if runner == "django" else "test_detached.py"
    (root / path).write_text(
        "import os\nimport subprocess\nimport sys\nimport time\nimport unittest\n"
        "from pathlib import Path\n\n"
        "class Detached(unittest.TestCase):\n"
        "    def test_service(self):\n"
        "        service = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(600)'],\n"
        "                                   start_new_session=True, env={})\n"
        "        assert os.getsid(service.pid) == service.pid\n"
        "        Path('service.pid').write_text(str(service.pid))\n"
        + ("        time.sleep(600)\n" if ending != "normal" else "")
    )
    process = subprocess.Popen(
        [sys.executable, "-m", "deflake", path, "--runner", runner, "-n", "1", "-t", "15"],
        cwd=root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    alive = False
    pid = None
    try:
        if ending == "interrupt":
            deadline = time.monotonic() + 30
            while not (root / "service.pid").exists() and time.monotonic() < deadline:
                assert process.poll() is None, process.communicate()
                time.sleep(0.02)

            assert (root / "service.pid").exists(), "the test never started its service"
            process.send_signal(signal.SIGINT)

        out, err = process.communicate(timeout=60)
        assert process.returncode == {"normal": 0, "timeout": 1, "interrupt": 130}[ending], out + err
        pid = int((root / "service.pid").read_text())
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.02)
        else:
            alive = True
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()
        if (root / "service.pid").exists():
            pid = int((root / "service.pid").read_text())
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)

    assert not alive, f"detached service {pid} outlived deflake ({runner}, {ending})"


def spawns_sessions() -> bool:
    # Some Python builds have posix_spawn without its setsid option
    try:
        pid = os.posix_spawn(sys.executable, [sys.executable, "-c", "pass"], {}, setsid=True)
    except (AttributeError, NotImplementedError):
        return False

    os.waitpid(pid, 0)

    return True


@pytest.mark.parametrize("api", ["fork", "posix_spawn"])
def test_low_level_detached_processes_remain_owned(project: Project, deflake: Deflake, api: str) -> None:
    root = project("pytest_cases")
    (root / "test_fork.py").write_text(
        "import os\nimport time\nfrom pathlib import Path\n\n"
        "def test_daemon():\n"
        "    child = os.fork()\n"
        "    if child == 0:\n"
        "        os.setsid()\n"
        "        if os.fork():\n            os._exit(0)\n"
        "        Path('daemon.pid').write_text(str(os.getpid()))\n"
        "        time.sleep(600)\n        os._exit(0)\n"
        "    os.waitpid(child, 0)\n"
        "    while not Path('daemon.pid').exists():\n        time.sleep(0.01)\n"
    )
    if api == "posix_spawn":
        if not spawns_sessions():
            pytest.skip("this Python cannot start a session with posix_spawn")
        (root / "test_fork.py").write_text(
            "import os\nimport sys\nfrom pathlib import Path\n\n"
            "def test_service():\n"
            "    pid = os.posix_spawn(sys.executable,\n"
            "                         [sys.executable, '-c', 'import time; time.sleep(600)'],\n"
            "                         {}, setsid=True)\n"
            "    assert os.getsid(pid) == pid\n"
            "    Path('daemon.pid').write_text(str(pid))\n"
        )

    pid = None
    try:
        result = deflake("test_fork.py", "-n", "1", cwd=root)
        assert result.code == 0, result.out + result.err
        pid = int((root / "daemon.pid").read_text())
        time.sleep(0.5)
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    finally:
        if (root / "daemon.pid").exists():
            pid = int((root / "daemon.pid").read_text())
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)


def test_project_object_mutations_survive_changed_module_metadata(project: Project, deflake: Deflake) -> None:
    root = project("pytest_regressions")
    result = deflake("test_metadata_objects.py", "-n", "1", "-f", "json", cwd=root)
    assert result.code == 1, result.out + result.err
    mutation = next(row for row in result.json if row["name"] == "test_c_mutates_object")
    assert mutation["leaks"] == [{"message": "metadata_objects.OBJ was changed", "runs": 1}]


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS audit-token kernel API")
def test_kernel_rejects_stale_audit_token_for_a_live_helper():
    import ctypes

    from deflake import events, execution

    helper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        raw = events.audit_token(helper.pid)
        assert raw is not None
        token = (ctypes.c_uint * 8).from_buffer_copy(raw)
        # A single exec can increment the version; the high-bit change cannot coincide with it
        token[7] ^= 0x80000000
        library = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
        assert library.proc_signal_with_audittoken(ctypes.byref(token), signal.SIGKILL) != 0
        assert helper.poll() is None
        identity = events.process_identity(helper.pid)
        assert identity is not None
        execution.signal_owned(helper.pid, identity, signal.SIGKILL)
        assert helper.wait(timeout=5) == -signal.SIGKILL
    finally:
        if helper.poll() is None:
            helper.kill()
        helper.wait()


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS exec and audit-token API")
@pytest.mark.parametrize("via_registry", [False, True])
def test_exec_between_audit_token_and_signal_is_cleaned_up(tmp_path, monkeypatch, via_registry):
    from deflake import events, execution

    initial = tmp_path / "initial"
    ready = tmp_path / "exec-ready"
    readfd, writefd = os.pipe()
    after = f"from pathlib import Path; import time; Path({str(ready)!r}).touch(); time.sleep(60)"
    source = (
        "import os,sys; from pathlib import Path; "
        f"Path({str(initial)!r}).touch(); os.read({readfd},1); "
        f'os.execv(sys.executable,[sys.executable,"-c",{after!r}])'
    )
    process = subprocess.Popen([sys.executable, "-c", source], pass_fds=(readfd,), start_new_session=True)
    os.close(readfd)
    original_token = events.audit_token
    triggered = []

    def wait_ready(path):
        deadline = time.monotonic() + 10
        while not path.exists():
            assert time.monotonic() < deadline, "helper did not reach its handshake"
            time.sleep(0.01)

    def token_before_exec(pid):
        token = original_token(pid)
        if pid == process.pid and not triggered:
            assert token is not None
            triggered.append(pid)
            os.write(writefd, b"x")
            wait_ready(ready)

        return token

    try:
        wait_ready(initial)
        birth = events.process_identity(process.pid)
        assert birth is not None
        monkeypatch.setattr(events, "audit_token", token_before_exec)
        if via_registry:
            registry = tmp_path / "processes"
            events.append(registry, pid=process.pid, identity=birth)
            execution.stop_descendants(registry)
        else:
            execution.signal_owned(process.pid, birth, signal.SIGKILL)
        assert triggered == [process.pid]
        assert process.wait(timeout=5) == -signal.SIGKILL
    finally:
        os.close(writefd)
        if process.poll() is None:
            process.kill()
        process.wait()


@pytest.mark.parametrize(
    ("source", "fixtures"),
    [
        (
            'import pytest\n\n\n@pytest.fixture(scope="session")\ndef service():\n'
            '    yield\n    raise RuntimeError("stuck")\n\n\n'
            "def test_a(service):\n    pass\n\n\ndef test_b(service):\n    pass\n",
            ["service (session scope)"],
        ),
        (
            'import pytest\n\n\n@pytest.fixture(scope="session")\ndef service(request):\n'
            "    request.addfinalizer(lambda: 1 / 0)\n\n\n"
            "def test_a(service):\n    pass\n\n\ndef test_b(service):\n    pass\n",
            ["service (session scope)"],
        ),
        (
            'import pytest\n\n\n@pytest.fixture(scope="session")\ndef service():\n'
            '    yield\n    raise RuntimeError("stuck")\n\n\n'
            '@pytest.fixture(scope="module")\ndef cache():\n    yield\n    raise OSError("full")\n\n\n'
            "def test_a(service, cache):\n    pass\n\n\ndef test_b(service, cache):\n    pass\n",
            ["service (session scope)", "cache (module scope)"],
        ),
        (
            'def teardown_module():\n    raise RuntimeError("stuck")\n\n\n'
            "def test_a():\n    pass\n\n\ndef test_b():\n    pass\n",
            ["(module scope)"],
        ),
        (
            "import unittest\n\n\nclass Wide(unittest.TestCase):\n"
            "    @classmethod\n    def tearDownClass(cls):\n        raise RuntimeError('stuck')\n\n"
            "    def test_a(self):\n        pass\n\n    def test_b(self):\n        pass\n",
            ["(class scope)"],
        ),
    ],
)
def test_a_failed_teardown_of_a_wider_fixture_is_outside_of_tests(
    project: Project,
    deflake: Deflake,
    source: str,
    fixtures: list[str],
) -> None:
    root = project("pytest_cases")
    (root / "test_wide.py").write_text(source)
    result = deflake("test_wide.py", "-n", "3", cwd=root)
    assert result.code == 1, result.out + result.err
    assert "errors outside of tests: failed 3 of 3 runs (100%)" in result.out, result.out
    # Fixtures pytest makes of xunit and unittest functions have names of its own
    teardowns = [line for line in result.out.splitlines() if "error in teardown of fixture" in line]
    for fixture in fixtures:
        assert any(f"{fixture}:" in line for line in teardowns), result.out

    assert result.out.count("failed 0 of 3 runs") == 2, result.out


def test_an_error_of_a_test_torn_down_with_a_wider_fixture_stays_its_own(
    project: Project,
    deflake: Deflake,
) -> None:
    root = project("pytest_cases")
    (root / "test_wide.py").write_text(
        'import pytest\n\n\n@pytest.fixture(scope="session")\ndef service(request):\n'
        '    request.addfinalizer(lambda: 1 / 0)\n    request.addfinalizer(lambda: {}["key"])\n\n\n'
        '@pytest.fixture\ndef own():\n    yield\n    raise OSError("mine")\n\n\n'
        "def test_a(service):\n    pass\n\n\ndef test_b(service, own):\n    pass\n"
    )
    result = deflake("test_wide.py", "-n", "4", cwd=root)
    assert result.code == 1, result.out + result.err
    # pytest 8.0 raises only the first error of the finalizers of a fixture, later ones a group of all
    assert "error in teardown of fixture service (session scope)" in result.out, result.out
    assert "test_b: failed 4 of 4 runs" in result.out, result.out
    # The same error whether test_b tore the session down or not, alone included
    assert "ExceptionGroup" not in result.out, result.out
    assert "error in teardown: OSError: mine" in result.out, result.out


def test_a_detached_service_group_is_stopped_after_a_timeout(project: Project, deflake: Deflake) -> None:
    root = project("pytest_cases")
    member = "import time; time.sleep(600)"
    service = (
        "import subprocess, sys, time; "
        f"children = [subprocess.Popen([sys.executable, '-c', {member!r}]) for _ in range(2)]; "
        "import os; pids = [os.getpid(), *(child.pid for child in children)]; "
        "open('members', 'a').write(''.join(f'{pid}\\n' for pid in pids)); time.sleep(600)"
    )
    (root / "test_service.py").write_text(
        "import subprocess\nimport sys\nimport time\n\n\n"
        "def test_hangs():\n"
        # With no environment the members do not register themselves: only the group stops them
        f"    subprocess.Popen([sys.executable, '-c', {service!r}], start_new_session=True, env={{}})\n"
        "    time.sleep(600)\n"
    )
    # Unknown until checked: the processes left to stop when the test fails before it
    running: list[int] | None = None
    try:
        result = deflake("test_service.py", "-n", "1", "-t", "5", cwd=root)
        assert "timed out" in result.out, result.out + result.err
        # The service and the members of its group, which the service itself started
        assert (root / "members").exists(), result.out + result.err
        members = [int(line) for line in (root / "members").read_text().split()]
        # Appended by every run of the test, alone included
        assert len(members) >= 3, result.out
        deadline = time.monotonic() + 5
        while (running := [pid for pid in members if is_running(pid)]) and time.monotonic() < deadline:
            time.sleep(0.05)

        assert not running, f"processes of the service's group are still running: {running}"
    finally:
        # Only processes not yet seen gone: a pid seen gone may be another's by now
        if running is None and (root / "members").exists():
            running = [int(line) for line in (root / "members").read_text().split() if is_running(int(line))]

        for pid in running or []:
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)


def is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    # A process of another user has the pid by now
    except (ProcessLookupError, PermissionError):
        return False

    return True


def test_an_error_of_a_test_failed_in_its_fixture_reads_the_same_torn_down_with_a_wider_one(
    project: Project,
    deflake: Deflake,
) -> None:
    root = project("pytest_cases")
    (root / "test_wide.py").write_text(
        'import pytest\n\n\n@pytest.fixture(scope="session")\ndef service():\n'
        '    yield\n    raise RuntimeError("stuck")\n\n\n'
        '@pytest.fixture\ndef own():\n    yield\n    pytest.fail("mine")\n\n\n'
        "def test_a(service):\n    pass\n\n\ndef test_b(service, own):\n    pass\n"
    )
    result = deflake("test_wide.py", "-n", "4", cwd=root)
    assert "error in teardown: Failed: mine\n      at test_wide.py:13" in result.out, result.out
    assert "outcomes.py" not in result.out, result.out


@pytest.mark.parametrize(
    "source",
    [
        "import os\n\n\ndef setup_module():\n    os.environ['SETUP_LEAK'] = '1'\n\n\n"
        "def test_a():\n    pass\n",
        "import os\nimport unittest\n\n\nclass Wide(unittest.TestCase):\n"
        "    @classmethod\n    def setUpClass(cls):\n        os.environ['SETUP_LEAK'] = '1'\n\n"
        "    def test_a(self):\n        pass\n",
    ],
)
def test_what_a_setup_of_the_module_or_class_leaves_is_outside_of_tests(
    project: Project,
    deflake: Deflake,
    source: str,
) -> None:
    root = project("pytest_cases")
    (root / "test_setup.py").write_text(source)
    result = deflake("test_setup.py", "-n", "2", cwd=root)
    assert result.code == 1, result.out + result.err
    assert "outside its tests: os.environ['SETUP_LEAK'] was set" in result.out, result.out


def test_a_change_of_a_wider_fixture_is_not_proven_a_cause_of_the_test_that_set_it_up(
    project: Project,
    deflake: Deflake,
) -> None:
    root = project("pytest_cases")
    (root / "test_wide_cause.py").write_text(
        "import os\n\nimport pytest\n\n\n"
        '@pytest.fixture(scope="module")\ndef service():\n    os.environ["WIDE_FLAG"] = "1"\n\n\n'
        "def test_a_uses(service):\n    pass\n\n\n"
        'def test_b_victim():\n    assert os.environ.get("WIDE_FLAG") is None\n'
    )
    # The victim fails in half of the orders, so 20 runs miss it once in a million
    result = deflake("test_wide_cause.py", "-n", "20", "-f", "json", cwd=root)
    victim = next(row for row in result.json if row["name"] == "test_b_victim")
    assert victim["culprits"], result.out
    # The leak is shown as the fixture's, outside of its tests: test_a_uses did not make it
    assert not any(cause["key"] == "WIDE_FLAG" for cause in victim["causes"]), result.out
