"""Causal controls must finish normally and genuinely pass on every runner."""

import json
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

Project = Callable[[str], Path]

# Only this harness changes the wait budget: startup remains unbounded by the short exit timeout
# execution.start still takes its real TimeoutExpired path after the victim's finished event
WAIT_FOR_VICTIM = """
import time
from pathlib import Path
from deflake import execution, events, result_file
original_launch = execution.launched
def launch(command, env, log):
    process = original_launch(command, env, log)
    original_wait = process.wait
    output = Path(env[events.OUTPUT_KEY])
    armed = False
    def wait(timeout=None):
        nonlocal armed
        if timeout is not None and not armed:
            deadline = time.monotonic() + 60
            while not output.exists() or not any(
                row.get('id', '').endswith('test_b_victim') for row in result_file.read(output)
            ):
                assert time.monotonic() < deadline, 'the victim never finished before the exit timeout'
                time.sleep(0.01)
            armed = True
            timeout = 0.05
        return original_wait(timeout)
    process.wait = wait
    return process
execution.launched = launch
"""


def run(root: Path, runner: str, path: str, timeout: bool = False):
    command = [
        sys.executable,
        "-c",
        (WAIT_FOR_VICTIM if timeout else "")
        + "\nimport random; random.sample=lambda population,k:list(range(k)); "
        "from deflake.cli import main; raise SystemExit(main())",
        path,
        "--runner",
        runner,
        "-n",
        "1",
        "-f",
        "json",
    ]
    if timeout:
        command += ["-t", "2"]

    result = subprocess.run(
        command,
        cwd=root,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    rows = json.loads(result.stdout)
    victim = next(row for row in rows if row["name"].endswith("test_b_victim"))
    assert victim["causes"], rows

    return victim["causes"]


@pytest.mark.parametrize("runner", ["pytest", "unittest", "django"])
def test_expected_failure_does_not_confirm_environment_repair(project: Project, runner: str) -> None:
    root = project("pytest_regressions" if runner == "pytest" else runner)
    path = "shop/test_expected_cause.py" if runner == "django" else "test_expected_cause.py"
    causes = run(root, runner, path)
    cause = next(cause for cause in causes if cause["key"] == "CAUSE_FLAG")
    assert not cause["confirmed"], causes
    assert cause["repair"] == ["expected_failure"] * 2


@pytest.mark.parametrize("runner", ["pytest", "unittest", "django"])
def test_timeout_after_victim_does_not_confirm_environment_cause(project: Project, runner: str) -> None:
    root = project("pytest_regressions" if runner == "pytest" else runner)
    path = "shop/test_timeout_cause.py" if runner == "django" else "test_timeout_cause.py"
    (root / path).write_text(
        "import atexit\nimport time\nimport os\nimport unittest\n"
        "time.sleep(3)\n"
        "atexit.register(time.sleep, 600)\n\n"
        "class Cause(unittest.TestCase):\n"
        "    def test_a_pollutes(self):\n        os.environ['CAUSE_FLAG'] = 'leaked'\n"
        "    def test_b_victim(self):\n        self.assertIsNone(os.getenv('CAUSE_FLAG'))\n"
    )
    causes = run(root, runner, path, timeout=True)
    assert all(not cause["confirmed"] for cause in causes), causes
    assert causes[0]["baseline"] == ["interrupted"] * 2


@pytest.mark.parametrize("runner", ["unittest", "django"])
@pytest.mark.parametrize("expected_failure", [False, True])
def test_module_scalar_causes_use_native_runner_outcomes(
    project: Project,
    runner: str,
    expected_failure: bool,
) -> None:
    root = project(runner)
    (root / "scalar_state.py").write_text("FLAG = 'original'\n")
    path = "shop/test_scalar_cause.py" if runner == "django" else "test_scalar_cause.py"
    source = (
        "import scalar_state\nimport unittest\n\n"
        "class Cause(unittest.TestCase):\n"
        "    def test_a_pollutes(self):\n        scalar_state.FLAG = 'leaked'\n"
    )
    if expected_failure:
        source += (
            "    def setUp(self):\n"
            "        if self._testMethodName == 'test_b_victim' and scalar_state.FLAG == 'leaked':\n"
            "            raise RuntimeError('original failure')\n"
            "    @unittest.expectedFailure\n"
            "    def test_b_victim(self):\n        raise ValueError('different expected failure')\n"
        )
    else:
        source += "    def test_b_victim(self):\n        self.assertEqual(scalar_state.FLAG, 'original')\n"

    (root / path).write_text(source)
    causes = run(root, runner, path)
    cause = next(cause for cause in causes if cause["key"] == "scalar_state.FLAG")
    assert cause["detector"] == "module"
    assert cause["confirmed"] is not expected_failure
    assert cause["repair"] == (["expected_failure"] * 2 if expected_failure else ["passed"] * 2)


@pytest.mark.parametrize("code", [2, 3, 4])
def test_session_error_after_finished_rows_cannot_confirm_cause(project: Project, code: int):
    root = project("pytest_regressions")
    with (root / "conftest.py").open("a") as file:
        file.write(
            "\nimport pytest\n\ndef pytest_sessionfinish(session, exitstatus):\n"
            f'    pytest.exit("session did not finish normally", returncode={code})\n'
        )

    causes = run(root, "pytest", "test_environment_cause.py")
    assert all(not cause["confirmed"] for cause in causes)
    assert all(cause["baseline"] == ["interrupted"] * 2 for cause in causes)


@pytest.mark.parametrize("runner", ["unittest", "django"])
def test_module_teardown_error_cannot_confirm_cause(project: Project, runner: str):
    root = project(runner)
    path = "shop/test_outside_cause.py" if runner == "django" else "test_outside_cause.py"
    (root / path).write_text(
        "import os\nimport unittest\n\n"
        'def tearDownModule():\n    raise RuntimeError("module teardown failed")\n\n'
        "class Cause(unittest.TestCase):\n"
        '    def test_a_pollutes(self):\n        os.environ["CAUSE_FLAG"] = "leaked"\n'
        '    def test_b_victim(self):\n        self.assertIsNone(os.getenv("CAUSE_FLAG"))\n'
    )
    causes = run(root, runner, path)
    assert all(not cause["confirmed"] for cause in causes)
    assert all(cause["baseline"] == ["interrupted"] * 2 for cause in causes)


@pytest.mark.parametrize("runner", ["pytest", "django"])
@pytest.mark.parametrize("finalization_error", [True, False])
def test_late_runner_error_cannot_hide_behind_a_failed_culprit(project, runner, finalization_error):
    root = project("pytest_regressions" if runner == "pytest" else "django")
    path = "test_late_error.py" if runner == "pytest" else "shop/test_late_error.py"
    (root / path).write_text(
        "import os\nimport unittest\n\n"
        "class Cause(unittest.TestCase):\n"
        "    def test_a_pollutes(self):\n"
        '        os.environ["LATE_CAUSE"] = "leaked"\n'
        '        self.fail("independent culprit failure")\n'
        "    def test_b_victim(self):\n"
        '        self.assertIsNone(os.getenv("LATE_CAUSE"), "polluted victim")\n'
    )
    if finalization_error and runner == "pytest":
        with (root / "conftest.py").open("a") as file:
            file.write(
                "\nimport pytest\n\ndef pytest_sessionfinish(session, exitstatus):\n"
                '    pytest.exit("late finalization failed", returncode=1)\n'
            )

    if finalization_error and runner == "django":
        (root / "project" / "late_runner.py").write_text(
            "from django.test.runner import DiscoverRunner\n\n"
            "class LateRunner(DiscoverRunner):\n"
            "    def teardown_test_environment(self, **kwargs):\n"
            "        super().teardown_test_environment(**kwargs)\n"
            '        raise RuntimeError("late finalization failed")\n'
        )
        with (root / "project" / "settings.py").open("a") as file:
            file.write('\nTEST_RUNNER = "project.late_runner.LateRunner"\n')

    command = [
        sys.executable,
        "-c",
        "import random; random.sample=lambda population,k:list(range(k)); "
        "from deflake.cli import main; raise SystemExit(main())",
        path,
        "--runner",
        runner,
        "-n",
        "1",
        "-f",
        "json",
    ]
    result = subprocess.run(
        command,
        cwd=root,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    rows = json.loads(result.stdout)
    victim = next(row for row in rows if row["name"].endswith("test_b_victim"))
    causes = [cause for cause in victim["causes"] if cause["key"] == "LATE_CAUSE"]
    assert causes, rows
    if finalization_error:
        assert all(not cause["confirmed"] for cause in causes), causes
        assert all(cause["baseline"] == ["interrupted"] * 2 for cause in causes)
        assert any(
            "late finalization failed" in error["message"] for row in rows for error in row["errors"]
        ), rows
    else:
        assert any(cause["confirmed"] for cause in causes), causes
        assert causes[0]["repair"] == ["passed"] * 2


@pytest.mark.parametrize("runner", ["pytest", "unittest"])
def test_a_cause_is_confirmed_when_the_error_shows_the_state_other_tests_change(
    project: Project,
    runner: str,
) -> None:
    root = project("pytest_regressions" if runner == "pytest" else runner)
    # The assert shows the whole environment, which differs between the run of the file and the
    # experiment, and the culprit leaves two keys, which putting one back moves in it
    (root / "test_shown_cause.py").write_text(
        "import os\nimport unittest\n\n\n"
        "class Cause(unittest.TestCase):\n"
        "    def test_a_pollutes(self):\n"
        "        os.environ['CAUSE_FLAG'] = 'leaked'\n        os.environ['OTHER_FLAG'] = 'leaked'\n\n"
        "    def test_b_victim(self):\n        assert 'CAUSE_FLAG' not in os.environ, dict(os.environ)\n\n"
        "    def test_c_unrelated(self):\n        os.environ['UNRELATED_FLAG'] = 'leaked'\n"
    )
    causes = run(root, runner, "test_shown_cause.py")
    cause = next(cause for cause in causes if cause["key"] == "CAUSE_FLAG")
    assert cause["confirmed"], causes
    other = next(cause for cause in causes if cause["key"] == "OTHER_FLAG")
    assert not other["confirmed"], causes


def test_a_counter_several_tests_raise_is_confirmed_as_the_cause(project: Project) -> None:
    root = project("pytest_regressions")
    (root / "counter_state.py").write_text("CALLS = 0\n")
    (root / "test_counter_cause.py").write_text(
        "import counter_state\n\n\n"
        "def test_a_pollutes():\n    counter_state.CALLS += 1\n\n\n"
        "def test_a_pollutes_again():\n    counter_state.CALLS += 1\n\n\n"
        "def test_b_victim():\n    assert counter_state.CALLS == 0\n"
    )
    causes = run(root, "pytest", "test_counter_cause.py")
    assert any(cause["key"] == "counter_state.CALLS" and cause["confirmed"] for cause in causes), causes


PROCESS_CAUSES = {
    "the working directory": (
        "os.chdir('subdirectory')",
        "assert not os.getcwd().endswith('subdirectory')",
    ),
    "sys.path": ("sys.path.insert(0, '/nowhere')", "assert '/nowhere' not in sys.path"),
    "sys.getrecursionlimit()": ("sys.setrecursionlimit(123456)", "assert sys.getrecursionlimit() != 123456"),
    "socket.getdefaulttimeout()": (
        "socket.setdefaulttimeout(7)",
        "assert socket.getdefaulttimeout() is None",
    ),
    "decimal.getcontext()": ("decimal.getcontext().prec = 3", "assert decimal.getcontext().prec == 28"),
    "the handler of SIGUSR1": (
        "signal.signal(signal.SIGUSR1, lambda *_: None)",
        "assert signal.getsignal(signal.SIGUSR1) is signal.SIG_DFL",
    ),
    "logging.disable()": (
        "logging.disable(logging.CRITICAL)",
        "assert logging.getLogger().isEnabledFor(logging.ERROR)",
    ),
    "the handlers of logger 'app'": (
        "logging.getLogger('app').addHandler(logging.StreamHandler())",
        "assert not logging.getLogger('app').handlers",
    ),
    "the level of logger 'app'": (
        "logging.getLogger('app').setLevel(logging.CRITICAL)",
        "assert logging.getLogger('app').isEnabledFor(logging.ERROR)",
    ),
}


@pytest.mark.parametrize("state", PROCESS_CAUSES)
def test_state_of_the_process_is_confirmed_as_the_cause(project: Project, state: str) -> None:
    root = project("pytest_regressions")
    (root / "subdirectory").mkdir()
    change, check = PROCESS_CAUSES[state]
    (root / "test_process_cause.py").write_text(
        "import decimal\nimport logging\nimport os\nimport signal\nimport socket\nimport sys\n\n\n"
        f"def test_a_pollutes():\n    {change}\n\n\n"
        f"def test_b_victim():\n    {check}\n"
    )
    causes = run(root, "pytest", "test_process_cause.py")
    assert any(
        cause["detector"] == "process" and cause["key"] == state and cause["confirmed"] for cause in causes
    ), causes


# The kind of state, how the culprit leaves it and how the victim fails with it
STOPPED_CAUSES = {
    "mock.patch of os.getcwd": (
        "patch",
        "mock.patch('os.getcwd', return_value='/patched').start()",
        "assert os.getcwd() != '/patched'",
    ),
    "mock.patch of os.environ": (
        "patch",
        "mock.patch.dict(os.environ, {'DEFLAKE_MODE': 'patched'}).start()",
        "assert os.environ.get('DEFLAKE_MODE') != 'patched'",
    ),
    "freezegun": (
        "clock",
        "freezegun.freeze_time('2000-01-01').start()",
        "assert datetime.date.today().year != 2000",
    ),
}


@pytest.mark.parametrize("state", STOPPED_CAUSES)
def test_a_patch_or_frozen_time_left_started_is_confirmed_as_the_cause(project: Project, state: str) -> None:
    root = project("pytest_regressions")
    kind, change, check = STOPPED_CAUSES[state]
    (root / "test_stopped_cause.py").write_text(
        "import datetime\nimport os\nfrom unittest import mock\n\nimport freezegun\n\n\n"
        f"def test_a_pollutes():\n    {change}\n\n\n"
        f"def test_b_victim():\n    {check}\n"
    )
    causes = run(root, "pytest", "test_stopped_cause.py")
    assert any(
        cause["detector"] == kind and cause["key"] == state and cause["confirmed"] for cause in causes
    ), causes


DJANGO_CAUSES = {
    "cache 'default'": ("cache.set('discount', 50)", "self.assertIsNone(cache.get('discount'))"),
    "settings.FEATURE_FLAG": (
        "settings.FEATURE_FLAG = True\n        assert settings.FEATURE_FLAG",
        "self.assertFalse(getattr(settings, 'FEATURE_FLAG', False))",
    ),
    "settings.DEBUG": (
        "override_settings(DEBUG=True).enable()",
        "self.assertFalse(settings.DEBUG)",
    ),
    "settings.CACHES": (
        "settings.CACHES['default']['TIMEOUT'] = 1",
        "self.assertNotIn('TIMEOUT', settings.CACHES['default'])",
    ),
    "the current time zone": (
        "timezone.activate('Asia/Tokyo')",
        "self.assertEqual(str(timezone.get_current_timezone()), settings.TIME_ZONE)",
    ),
    "the URLconf of the thread": ("set_urlconf('shop.models')", "self.assertIsNone(get_urlconf())"),
    "the active language": (
        "translation.activate('de')",
        "self.assertEqual(translation.get_language(), 'en-us')",
    ),
    "a receiver of django.core.signals.request_started": (
        "request_started.connect(refuse, weak=False)",
        "request_started.send(sender=None)",
    ),
}


@pytest.mark.parametrize("state", DJANGO_CAUSES)
def test_state_of_django_is_confirmed_as_the_cause(project: Project, state: str) -> None:
    root = project("django")
    change, check = DJANGO_CAUSES[state]
    (root / "shop" / "test_django_cause.py").write_text(
        "from django.conf import settings\nfrom django.core.cache import cache\n"
        "from django.core.signals import request_started\n"
        "from django.test import SimpleTestCase, override_settings\n"
        "from django.urls import get_urlconf, set_urlconf\n"
        "from django.utils import timezone, translation\n\n\n"
        "def refuse(**kwargs):\n    raise RuntimeError('refused')\n\n\n"
        "class Cause(SimpleTestCase):\n"
        f"    def test_a_pollutes(self):\n        {change}\n\n"
        f"    def test_b_victim(self):\n        {check}\n"
    )
    causes = run(root, "django", "shop/test_django_cause.py")
    assert any(
        cause["detector"] == "django" and cause["key"] == state and cause["confirmed"] for cause in causes
    ), causes
