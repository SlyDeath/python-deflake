"""The deflake command on unittest files"""

import json
import os
import shlex
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from tests.e2e.conftest import Deflake

Project = Callable[[str], Path]


def test_finds_the_culprit_of_an_order_dependent_test(project: Project, deflake: Deflake) -> None:
    # The victim fails in half of the orders, so 20 runs miss it once in a million
    result = deflake("test_order.py", "-n", "20", cwd=project("unittest"))
    assert result.code == 1, result.err
    victim = result.out.split("OrderTest.test_victim: ")[1]
    assert "verdict: fails only after other tests (alone: failed 0 of 20 runs (0%))" in victim
    assert "culprit: OrderTest.test_pollutes\n" in victim
    assert "python -m deflake.unittest_runner test_order.py" in victim
    assert "AssertionError: True is not false\n      at test_order.py:13" in victim


def test_reproduce_command_shows_the_failure(project: Project, deflake: Deflake) -> None:
    root = project("unittest")
    result = deflake("test_order.py", "-n", "20", "-f", "json", cwd=root)
    victim = next(test for test in result.json if test["name"] == "OrderTest.test_victim")
    variable, *command = shlex.split(victim["culprits"]["command"])
    name, value = variable.split("=")
    command[0] = sys.executable
    run = subprocess.run(command, cwd=root, env={**os.environ, name: value}, capture_output=True, text=True)
    assert run.returncode == 1
    assert "FAIL: test_victim (test_order.OrderTest.test_victim)" in run.stderr


def test_report(project: Project, deflake: Deflake) -> None:
    result = deflake("test_order.py", "-n", "3", "-f", "json", cwd=project("unittest"))
    tests = {test["name"]: test for test in result.json}
    assert tests["OtherTest.test_one"]["id"] == "test_order.OtherTest.test_one"
    assert tests["OtherTest.test_one"]["verdict"] == "stable"
    assert "OtherTest.test_skipped" not in tests
    assert [leak["message"] for leak in tests["OrderTest.test_pollutes"]["leaks"]] == [
        "os.environ['DEFLAKE_FIXTURE_FLAG'] was set",
        "test_order.STATE was changed",
    ]


def test_selectors(project: Project, deflake: Deflake) -> None:
    root = project("unittest")
    by_name = deflake("test_order.py::OtherTest", "-n", "1", "-f", "json", cwd=root)
    assert [test["name"] for test in by_name.json] == ["OtherTest.test_one"], by_name.err
    by_method = deflake("test_order.py::test_unrelated", "-n", "1", "-f", "json", cwd=root)
    assert [test["name"] for test in by_method.json] == ["OrderTest.test_unrelated"]
    by_line = deflake("test_order.py:16", "-n", "1", "-f", "json", cwd=root)
    assert [test["name"] for test in by_line.json] == ["OrderTest.test_unrelated"]


def test_errors_outside_of_tests(project: Project, deflake: Deflake) -> None:
    root = project("unittest")
    (root / "test_class_setup.py").write_text(
        "import os\nimport unittest\n\n\nclass Broken(unittest.TestCase):\n"
        "    @classmethod\n    def setUpClass(cls):\n        raise RuntimeError('no database')\n\n"
        "    def test_never(self):\n        pass\n\n\n"
        "class Leaky(unittest.TestCase):\n    @classmethod\n    def setUpClass(cls):\n"
        "        os.environ['CLASS_LEAK'] = '1'\n\n    def test_fine(self):\n        pass\n"
    )
    result = deflake("test_class_setup.py", "-n", "2", "-f", "json", cwd=root)
    outside = next(test for test in result.json if test["id"] == "(outside of tests)")
    assert outside["runs"]["failures"] == 2
    assert "RuntimeError: no database" in outside["errors"][0]["message"]
    assert outside["leaks"] == [
        {
            "message": "class or module setup (setUpClass, setUpModule), outside its tests: "
            "os.environ['CLASS_LEAK'] was set",
            "runs": 2,
        }
    ]


def test_a_file_that_cannot_be_imported(project: Project, deflake: Deflake) -> None:
    root = project("unittest")
    (root / "test_broken.py").write_text("import unittest\nimport no_such_module\n")
    result = deflake("test_broken.py", "-n", "2", cwd=root)
    assert result.code == 1
    assert "errors outside of tests: failed 2 of 2 runs (100%)" in result.out
    assert "error importing test_broken.py" in result.out
    assert "No module named 'no_such_module'" in result.out


def test_the_runner_is_chosen_by_the_file(project: Project, deflake: Deflake) -> None:
    root = project("unittest")
    (root / "test_functions.py").write_text("def test_plain():\n    pass\n")
    result = deflake("test_functions.py", "-n", "1", "-f", "json", cwd=root)
    assert json.loads(result.out)[0]["id"] == "test_functions.py::test_plain"
    forced = deflake("test_order.py::OtherTest", "-n", "1", "-r", "pytest", "-f", "json", cwd=root)
    assert [test["id"] for test in forced.json] == ["test_order.py::OtherTest::test_one"]


def test_an_async_test_is_watched_until_its_loop_is_closed(project: Project, deflake: Deflake) -> None:
    root = project("unittest")
    (root / "test_async.py").write_text(
        "import asyncio\nimport unittest\n\n\nclass AsyncTest(unittest.IsolatedAsyncioTestCase):\n"
        "    async def test_thread(self):\n        await asyncio.to_thread(sum, [1, 2])\n"
    )
    result = deflake("test_async.py", "-n", "2", cwd=root)
    assert result.out == "AsyncTest.test_thread: failed 0 of 2 runs (0%)\n", result.out + result.err


def test_a_class_that_skips_its_tests(project: Project, deflake: Deflake) -> None:
    root = project("unittest")
    (root / "test_skip_class.py").write_text(
        "import unittest\n\n\nclass Skipped(unittest.TestCase):\n    @classmethod\n"
        "    def setUpClass(cls):\n        raise unittest.SkipTest('no network')\n\n"
        "    def test_one(self):\n        pass\n"
    )
    result = deflake("test_skip_class.py", "-n", "2", cwd=root)
    assert result.code == 0, result.out + result.err
    assert result.err == "deflake: every test skipped itself, nothing to report\n"


def test_a_line_does_not_select_a_test_of_a_mixin(project: Project, deflake: Deflake) -> None:
    root = project("unittest")
    (root / "mixin.py").write_text("class Mixin:\n    def test_mixed(self):\n        pass\n")
    (root / "test_mixin.py").write_text(
        "import unittest\n\nfrom mixin import Mixin\n\n\nclass MixedTest(Mixin, unittest.TestCase):\n"
        "    def test_own(self):\n        pass\n"
    )
    # Line 2 holds the test of the mixin in mixin.py, and nothing in test_mixin.py
    result = deflake("test_mixin.py:7", "-n", "1", "-f", "json", cwd=root)
    assert [test["name"] for test in result.json] == ["MixedTest.test_own"], result.err
    whole_class = deflake("test_mixin.py:6", "-n", "1", "-f", "json", cwd=root)
    assert sorted(test["name"] for test in whole_class.json) == ["MixedTest.test_mixed", "MixedTest.test_own"]


def test_a_line_does_not_select_a_class_of_another_module(project: Project, deflake: Deflake) -> None:
    root = project("unittest")
    (root / "shared.py").write_text(
        "import unittest\n\n\nclass SharedTests(unittest.TestCase):\n    def test_one(self):\n"
        "        pass\n\n    def test_two(self):\n        pass\n"
    )
    (root / "test_imported.py").write_text(
        "import unittest\n\nfrom shared import SharedTests\n\n\nclass LocalTest(unittest.TestCase):\n"
        "    def test_local(self):\n        pass\n"
    )
    # Lines 6 and 8 hold tests of SharedTests in shared.py
    result = deflake("test_imported.py:6", "-n", "1", "-f", "json", cwd=root)
    assert [test["name"] for test in result.json] == ["LocalTest.test_local"], result.err
