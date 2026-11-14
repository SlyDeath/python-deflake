"""Leaks the detectors find in a pytest project"""

from collections.abc import Callable
from pathlib import Path

import pytest

from tests.e2e.conftest import Deflake

Project = Callable[[str], Path]


def test_a_private_slot_mutation_is_reported_as_a_leak(project: Project, deflake: Deflake) -> None:
    result = deflake("test_private_slots.py", "-n", "1", "-f", "json", cwd=project("pytest_regressions"))
    assert result.code == 1, result.out + result.err
    test = next(test for test in result.json if test["name"] == "test_pollutes")
    assert test["leaks"] == [{"message": "test_private_slots.STATE was changed", "runs": 1}]


EXPECTED = {
    "test_changes_cwd": ["the working directory was changed"],
    "test_mutates_a_module_global": ["settings.FEATURES was changed"],
    "test_assigns_a_module_global": ["settings.LIMIT was changed"],
    "test_starts_a_patch": ["mock.patch of os.getpid is still active (patch.start() without stop()?)"],
    "test_changes_sys_path": ["sys.path was changed"],
    "test_changes_a_logger": ["the level of logger 'fixture.logger' was changed"],
    # A library quiets the logger it makes, as the logging docs ask
    "test_imports_a_quiet_library": [],
    "test_logs_to_a_new_handler": ["the handlers of logger 'fixture.output' was changed"],
    "test_disables_logging": ["logging.disable() was left at level 40"],
    # pytest's warnings plugin puts the filters back after every test
    "test_changes_warning_filters": [],
    "test_leaves_a_thread_running": ["thread 'fixture-worker' is still running"],
    "test_changes_socket_timeout": ["socket.getdefaulttimeout() was changed"],
    "test_changes_decimal_context": ["decimal.getcontext() was changed"],
    "test_travels_in_time": ["time_machine is still travelling (travel().start() without stop()?)"],
    "test_changes_a_signal_handler": ["the handler of SIGUSR1 was changed"],
    "test_puts_everything_back": [],
    "test_uses_module_state": [],
    "test_uses_module_state_too": [],
    "test_uses_a_leaking_fixture": [],
    "test_sleeps_without_leaking": [],
}


def test_every_detector_reports_its_leak_on_the_test_that_made_it(project: Project, deflake: Deflake) -> None:
    result = deflake("test_leaks.py", "-n", "3", "-f", "json", cwd=project("pytest_leaks"))
    leaks = {test["name"]: [leak["message"] for leak in test["leaks"]] for test in result.json}
    assert {name: leaks.get(name) for name in EXPECTED} == EXPECTED


def test_a_module_fixture_that_does_not_clean_up_leaks_outside_of_tests(
    project: Project,
    deflake: Deflake,
) -> None:
    # Some seeds put the test that uses the fixture last, so the fixture is set up and torn down
    # during the same test
    result = deflake("test_leaks.py", "-n", "8", "-f", "json", cwd=project("pytest_leaks"))
    outside = next(test for test in result.json if test["id"] == "(outside of tests)")
    assert outside["leaks"] == [
        {
            "message": "fixture module_leak (module scope), outside its tests: "
            "os.environ['FIXTURE_MODULE_LEAK'] was set",
            "runs": 8,
        }
    ]


def test_freezegun_left_on(project: Project, deflake: Deflake) -> None:
    result = deflake("test_freezegun.py", "-n", "2", "-f", "json", cwd=project("pytest_leaks"))
    leaks = {test["name"]: [leak["message"] for leak in test["leaks"]] for test in result.json}
    assert leaks == {
        "test_does_not_freeze": [],
        "test_freezes_time": ["freezegun is still freezing time (freeze_time().start() without stop()?)"],
    }


def test_a_detector_of_the_project(project: Project, deflake: Deflake) -> None:
    root = project("pytest")
    (root / "conftest.py").write_text(
        "from deflake import leaks\n\nFLAGS = set()\n\n\n"
        "class Flags:\n    def snapshot(self):\n        return sorted(FLAGS)\n\n"
        "    def diff(self, before, after):\n"
        "        return [f'flag {flag} left on' for flag in sorted(set(after) - set(before))]\n\n\n"
        "leaks.detectors.append(Flags())\n"
    )
    (root / "test_flags.py").write_text(
        "from conftest import FLAGS\n\n\ndef test_sets_a_flag():\n    FLAGS.add('beta')\n"
    )
    result = deflake("test_flags.py", "-n", "2", cwd=root)
    assert "  leaks: flag beta left on (2 of 2 runs)\n" in result.out, result.out + result.err


@pytest.mark.parametrize("runner", ["pytest", "unittest", "django"])
def test_deleted_cwd_is_reported_before_module_cleanup(project: Project, deflake: Deflake, runner: str):
    root = project("pytest_regressions" if runner == "pytest" else runner)
    path = "shop/test_deleted_cwd.py" if runner == "django" else "test_deleted_cwd.py"
    (root / path).write_text(
        "import os\nimport tempfile\nimport unittest\n"
        "ORIGINAL = os.getcwd()\n"
        "def tearDownModule():\n    os.chdir(ORIGINAL)\n"
        "class Case(unittest.TestCase):\n"
        "    def test_deleted_directory(self):\n"
        "        with tempfile.TemporaryDirectory(dir=ORIGINAL) as directory:\n"
        "            os.chdir(directory)\n"
        "    def test_z_next(self):\n        pass\n"
    )
    result = deflake(path, "--runner", runner, "-n", "1", "-f", "json", cwd=root)
    assert result.code == 1, result.out + result.err
    test = next(row for row in result.json if row["name"].endswith("test_deleted_directory"))
    assert {"message": "the working directory was changed", "runs": 1} in test["leaks"]


def test_warning_filters_a_test_leaves_are_its_leak_without_the_warnings_plugin(
    project: Project,
    deflake: Deflake,
) -> None:
    root = project("pytest_leaks")
    with (root / "pytest.ini").open("a") as file:
        file.write("\naddopts = -p no:warnings\n")

    result = deflake("test_leaks.py::test_changes_warning_filters", "-n", "2", "-f", "json", cwd=root)
    test = next(test for test in result.json if test["name"] == "test_changes_warning_filters")
    assert [leak["message"] for leak in test["leaks"]] == [
        "warnings.filters was changed (simplefilter without catch_warnings?)"
    ], result.out
