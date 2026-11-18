"""The deflake command on Django projects, with manage.py test"""

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
    result = deflake("shop/tests.py", "-n", "20", cwd=project("django"))
    assert result.code == 1, result.err
    victim = result.out.split("CacheTest.test_victim: ")[1]
    assert "verdict: fails only after other tests (alone: failed 0 of 20 runs (0%))" in victim
    assert "culprit: CacheTest.test_pollutes\n" in victim
    assert "python manage.py test --testrunner deflake.django.Runner shop/tests.py" in victim
    assert "AssertionError: 50 is not None\n      at tests.py:12" in victim
    assert "  leaks: cache entry ('default', ':1:discount') was added (20 of 20 runs)\n" in result.out


def test_reproduce_command_shows_the_failure(project: Project, deflake: Deflake) -> None:
    root = project("django")
    result = deflake("shop/tests.py", "-n", "20", "-f", "json", cwd=root)
    victim = next(test for test in result.json if test["name"] == "CacheTest.test_victim")
    variable, *command = shlex.split(victim["culprits"]["command"])
    name, value = variable.split("=")
    command[0] = sys.executable
    run = subprocess.run(command, cwd=root, env={**os.environ, name: value}, capture_output=True, text=True)
    assert run.returncode == 1
    assert "FAIL: test_victim (shop.tests.CacheTest.test_victim)" in run.stderr


def test_leaks(project: Project, deflake: Deflake) -> None:
    result = deflake("shop/test_leaks.py", "-n", "2", "-f", "json", cwd=project("django"))
    leaks = {test["name"]: [leak["message"] for leak in test["leaks"]] for test in result.json}
    assert leaks == {
        "LeaksTest.test_activates_a_language": [
            "the active language was changed (activate() without deactivate()?)"
        ],
        "LeaksTest.test_activates_a_time_zone": [
            "the current time zone was changed (activate() without deactivate()?)"
        ],
        "LeaksTest.test_changes_a_setting": ["settings.DEBUG was changed"],
        "LeaksTest.test_clean": [],
        "LeaksTest.test_connects_a_receiver": [
            "a receiver of django.db.models.signals.post_save was connected and not disconnected"
        ],
        "LeaksTest.test_decorated": [],
        "LeaksTest.test_leaves_settings_overridden": ["settings.USE_TZ was changed"],
        "LeaksTest.test_leaves_the_same_settings_overridden": [
            "override_settings is still active (enable() without disable()?)"
        ],
        "LeaksTest.test_overrides_settings_properly": [],
    }


def test_rows_left_outside_of_a_transaction(project: Project, deflake: Deflake) -> None:
    result = deflake("shop/test_rows.py", "-n", "2", "-f", "json", cwd=project("django"))
    leaks = {test["name"]: [leak["message"] for leak in test["leaks"]] for test in result.json}
    assert leaks == {
        "PlainTest.test_leaves_a_row": ["rows were created in shop_product (1)"],
        "RolledBackTest.test_creates": [],
    }


def test_jobs_get_test_databases_of_their_own(project: Project, deflake: Deflake) -> None:
    root = project("django")
    settings = root / "project" / "settings.py"
    settings.write_text(
        settings.read_text().replace(
            '"NAME": "db.sqlite3"}',
            '"NAME": "db.sqlite3", "TEST": {"NAME": "test.sqlite3"}}',
        )
    )
    (root / "shop" / "test_names.py").write_text(
        "import os\nfrom django.db import connection\nfrom django.test import TestCase\n\n\n"
        "class NameTest(TestCase):\n    def test_name(self):\n"
        "        with open(f'name-{os.getpid()}', 'w') as file:\n"
        "            file.write(connection.settings_dict['NAME'])\n"
    )
    result = deflake("shop/test_names.py", "-n", "4", "-j", "2", cwd=root)
    assert result.code == 0, result.out + result.err
    names = {path.read_text() for path in root.glob("name-*")}
    assert names <= {"test.sqlite3_1", "test.sqlite3_2"}, names
    assert names


def test_a_project_runner_still_runs(project: Project, deflake: Deflake) -> None:
    root = project("django")
    (root / "project" / "runner.py").write_text(
        "import os\nfrom django.test.runner import DiscoverRunner\n\n\n"
        "class Runner(DiscoverRunner):\n    def setup_test_environment(self, **kwargs):\n"
        "        super().setup_test_environment(**kwargs)\n        os.environ['PROJECT_RUNNER'] = '1'\n"
    )
    settings = root / "project" / "settings.py"
    settings.write_text(settings.read_text() + 'TEST_RUNNER = "project.runner.Runner"\n')
    (root / "shop" / "test_runner.py").write_text(
        "import os\nfrom django.test import SimpleTestCase\n\n\n"
        "class RunnerTest(SimpleTestCase):\n    def test_runner(self):\n"
        "        self.assertEqual(os.environ.get('PROJECT_RUNNER'), '1')\n"
    )
    result = deflake("shop/test_runner.py", "-n", "2", cwd=root)
    assert result.out == "RunnerTest.test_runner: failed 0 of 2 runs (0%)\n", result.out + result.err


def test_a_line_selects_a_test(project: Project, deflake: Deflake) -> None:
    result = deflake("shop/tests.py:12", "-n", "1", "-f", "json", cwd=project("django"))
    assert [test["name"] for test in result.json] == ["CacheTest.test_victim"], result.err


def test_a_detector_left_out(project: Project, deflake: Deflake) -> None:
    root = project("django")
    settings = root / "project" / "settings.py"
    settings.write_text(
        settings.read_text()
        + "\nfrom deflake import leaks\nfrom deflake.django.leaks import Rows\n\n"
        + "leaks.excluded.append(Rows)\n"
    )
    result = deflake("shop/test_rows.py::PlainTest", "-n", "1", cwd=root)
    assert result.out == "PlainTest.test_leaves_a_row: failed 0 of 1 runs (0%)\n", result.out + result.err


def test_fixtures_a_transaction_test_case_loads_early(project: Project, deflake: Deflake) -> None:
    root = project("django")
    (root / "shop" / "fixtures").mkdir()
    (root / "shop" / "fixtures" / "products.json").write_text(
        '[{"model": "shop.product", "pk": 1, "fields": {"name": "tea"}}]'
    )
    (root / "shop" / "test_fixtures.py").write_text(
        "from django.test import TransactionTestCase\n\nfrom shop.models import Product\n\n\n"
        "class FixturesTest(TransactionTestCase):\n    fixtures = ['products.json']\n\n"
        "    def test_one(self):\n        self.assertEqual(Product.objects.count(), 1)\n\n"
        "    def test_two(self):\n        self.assertEqual(Product.objects.count(), 1)\n"
    )
    result = deflake("shop/test_fixtures.py", "-n", "2", cwd=root)
    assert result.out == (
        "FixturesTest.test_one: failed 0 of 2 runs (0%)\nFixturesTest.test_two: failed 0 of 2 runs (0%)\n"
    ), result.out + result.err


def test_an_error_setting_up_a_test_is_the_tests(project: Project, deflake: Deflake) -> None:
    root = project("django")
    (root / "shop" / "test_setup.py").write_text(
        "from django.test import TransactionTestCase\n\n\nclass SetupTest(TransactionTestCase):\n"
        "    calls = 0\n\n    @classmethod\n    def _fixture_setup(cls):\n        cls.calls += 1\n"
        "        if cls.calls > 1:\n            raise RuntimeError('broken fixture')\n\n"
        "    def test_one(self):\n        pass\n\n    def test_two(self):\n        pass\n"
    )
    result = deflake("shop/test_setup.py", "-n", "2", "-f", "json", cwd=root)
    tests = {test["name"]: test for test in result.json}
    assert "(outside of tests)" not in tests, result.out
    # The class sets up its first test early, so the second one fails, whichever it is
    assert sum(test["runs"]["failures"] for test in tests.values()) == 2
    errors = [error["message"] for test in tests.values() for error in test["errors"]]
    assert all(message.startswith("error in setup: RuntimeError: broken fixture") for message in errors)


def test_a_query_set_of_a_module_is_not_evaluated(project: Project, deflake: Deflake) -> None:
    root = project("django")
    (root / "shop" / "test_query.py").write_text(
        "from django.test import TestCase\n\nfrom shop.models import Product\n\n"
        "RECENT = Product.objects.all()\n\n\nclass QueryTest(TestCase):\n    def test_sees_the_row(self):\n"
        "        Product.objects.create(name='tea')\n        self.assertEqual(len(RECENT), 1)\n"
    )
    result = deflake("shop/test_query.py", "-n", "2", cwd=root)
    assert result.out == "QueryTest.test_sees_the_row: failed 0 of 2 runs (0%)\n", result.out + result.err


def test_a_receiver_gone_with_its_function_is_not_a_leak(project: Project, deflake: Deflake) -> None:
    root = project("django")
    (root / "shop" / "test_receivers.py").write_text(
        "from django.db.models.signals import post_save\nfrom django.test import TestCase\n\n"
        "from shop.models import Product\n\n\nclass ReceiverTest(TestCase):\n"
        "    def test_connects(self):\n        def handler(**kwargs):\n            pass\n\n"
        "        post_save.connect(handler)\n\n    def test_saves(self):\n"
        "        Product.objects.create(name='tea')\n"
    )
    result = deflake("shop/test_receivers.py", "-n", "6", "-f", "json", cwd=root)
    leaks = {test["name"]: [leak["message"] for leak in test["leaks"]] for test in result.json}
    assert leaks["ReceiverTest.test_saves"] == [], result.out


def test_a_skipped_test_leaves_the_early_setup_to_the_next(project: Project, deflake: Deflake) -> None:
    root = project("django")
    (root / "shop" / "fixtures").mkdir()
    (root / "shop" / "fixtures" / "products.json").write_text(
        '[{"model": "shop.product", "pk": 1, "fields": {"name": "tea"}}]'
    )
    (root / "shop" / "test_skip_first.py").write_text(
        "import unittest\n\nfrom django.test import TransactionTestCase\n\n\n"
        "class SkipTest(TransactionTestCase):\n    fixtures = ['products.json']\n\n"
        "    @unittest.skip('later')\n    def test_skipped(self):\n        pass\n\n"
        "    def test_one(self):\n        pass\n\n    def test_two(self):\n        pass\n"
    )
    result = deflake("shop/test_skip_first.py", "-n", "8", cwd=root)
    assert "rows were" not in result.out, result.out
    assert result.code == 0, result.out + result.err


def test_a_test_client_of_a_module_is_not_a_leak(project: Project, deflake: Deflake) -> None:
    root = project("django")
    (root / "shop" / "test_client.py").write_text(
        "from django.test import Client, SimpleTestCase\n\nclient = Client()\n\n\n"
        "class ClientTest(SimpleTestCase):\n    def test_get(self):\n        client.get('/missing/')\n\n"
        "    def test_other(self):\n        pass\n"
    )
    result = deflake("shop/test_client.py", "-n", "4", cwd=root)
    assert "client was changed" not in result.out, result.out
    assert result.code == 0, result.out + result.err


def test_a_project_runner_with_a_result_of_its_own(project: Project, deflake: Deflake) -> None:
    root = project("django")
    # As xmlrunner's Django runner does: its test runner makes the result with one more argument and
    # calls a method of its own result at the end
    (root / "project" / "runner.py").write_text(
        "import unittest\n\nfrom django.test.runner import DiscoverRunner\n\n\n"
        "class Result(unittest.TextTestResult):\n"
        "    def __init__(self, stream, descriptions, verbosity, times):\n"
        "        super().__init__(stream, descriptions, verbosity)\n        self.times = times\n\n"
        "    def generate_reports(self):\n        assert self.times == {}\n\n\n"
        "class TextRunner(unittest.TextTestRunner):\n"
        "    def __init__(self, resultclass=None, **kwargs):\n"
        "        super().__init__(resultclass=resultclass, **kwargs)\n"
        "        self.resultclass = resultclass or Result\n\n"
        "    def _makeResult(self):\n"
        "        return self.resultclass(self.stream, self.descriptions, self.verbosity, {})\n\n"
        "    def run(self, test):\n        result = super().run(test)\n"
        "        result.generate_reports()\n        return result\n\n\n"
        "class Runner(DiscoverRunner):\n    test_runner = TextRunner\n"
    )
    settings = root / "project" / "settings.py"
    settings.write_text(settings.read_text() + 'TEST_RUNNER = "project.runner.Runner"\n')
    result = deflake("shop/tests.py::ProductTest", "-n", "2", cwd=root)
    assert result.out == "ProductTest.test_creates: failed 0 of 2 runs (0%)\n", result.out + result.err


def test_rows_of_a_mirrored_database_are_counted_once(project: Project, deflake: Deflake) -> None:
    root = project("django")
    settings = root / "project" / "settings.py"
    settings.write_text(
        settings.read_text()
        + 'DATABASES["replica"] = {**DATABASES["default"], "TEST": {"MIRROR": "default"}}\n'
    )
    result = deflake("shop/test_rows.py::PlainTest", "-n", "2", cwd=root)
    assert result.out == (
        "PlainTest.test_leaves_a_row: failed 0 of 2 runs (0%)\n"
        "  leaks: rows were created in shop_product (1) (2 of 2 runs)\n"
    ), result.out + result.err


def test_a_live_server_is_not_a_leak_of_the_first_test(project: Project, deflake: Deflake) -> None:
    root = project("django")
    (root / "shop" / "test_live.py").write_text(
        "from django.test import LiveServerTestCase\n\n\n"
        "class LiveTest(LiveServerTestCase):\n    def test_one(self):\n        pass\n\n"
        "    def test_two(self):\n        pass\n"
    )
    result = deflake("shop/test_live.py", "-n", "4", cwd=root)
    assert "leaks" not in result.out, result.out + result.err
    assert result.code == 0, result.out + result.err


def test_a_test_runner_made_by_a_function(project: Project, deflake: Deflake) -> None:
    root = project("django")
    (root / "project" / "runner.py").write_text(
        "import functools\nimport unittest\n\nfrom django.test.runner import DiscoverRunner\n\n\n"
        "class Runner(DiscoverRunner):\n"
        "    test_runner = staticmethod(functools.partial(unittest.TextTestRunner, verbosity=0))\n"
    )
    settings = root / "project" / "settings.py"
    settings.write_text(settings.read_text() + 'TEST_RUNNER = "project.runner.Runner"\n')
    result = deflake("shop/tests.py::ProductTest", "-n", "2", cwd=root)
    assert result.out == "ProductTest.test_creates: failed 0 of 2 runs (0%)\n", result.out + result.err


def test_a_project_runner_that_bypasses_its_test_runner_is_told(project: Project, deflake: Deflake) -> None:
    root = project("django")
    (root / "project" / "runner.py").write_text(
        "import unittest\n\nfrom django.test.runner import DiscoverRunner\n\n\n"
        "class Runner(DiscoverRunner):\n"
        "    def run_suite(self, suite, **kwargs):\n        return unittest.TextTestRunner().run(suite)\n"
    )
    settings = root / "project" / "settings.py"
    settings.write_text(settings.read_text() + 'TEST_RUNNER = "project.runner.Runner"\n')
    result = deflake("shop/tests.py::ProductTest", "-n", "1", cwd=root)
    assert "runs the tests without its test_runner" in result.out + result.err, result.out + result.err


def test_a_test_asked_for_in_a_module_that_does_not_import(project: Project, deflake: Deflake) -> None:
    root = project("django")
    (root / "shop" / "test_broken.py").write_text("import nonexistent_module_xyz\n")
    result = deflake("shop/test_broken.py::BrokenTest", "-n", "1", cwd=root)
    assert "nonexistent_module_xyz" in result.out + result.err, result.out + result.err
    assert result.code == 2, result.out + result.err


def test_apps_in_a_folder_of_their_own(project: Project, deflake: Deflake) -> None:
    root = project("django")
    (root / "apps").mkdir()
    (root / "shop").rename(root / "apps" / "shop")
    manage = root / "manage.py"
    manage.write_text(
        manage.read_text().replace(
            'if __name__ == "__main__":',
            'sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "apps"))\n\n'
            'if __name__ == "__main__":',
        )
    )
    tests = root / "apps" / "shop" / "tests.py"
    tests.write_text(tests.read_text().replace("from shop.models import", "from .models import"))
    # The victim fails in half of the orders, so 20 runs miss it once in a million
    result = deflake("apps/shop/tests.py::CacheTest", "-n", "20", cwd=root)
    assert "culprit: CacheTest.test_pollutes" in result.out, result.out + result.err
    assert "manage.py test --testrunner deflake.django.Runner apps/shop/tests.py" in result.out


def test_a_test_database_that_cannot_be_created_means_the_tests_could_not_run(
    project: Project,
    deflake: Deflake,
) -> None:
    root = project("django")
    settings = root / "project" / "settings.py"
    settings.write_text(
        settings.read_text().replace(
            '"NAME": "db.sqlite3"}',
            '"NAME": "db.sqlite3", "TEST": {"NAME": "missing/test.sqlite3"}}',
        )
    )
    result = deflake("shop/tests.py", "-n", "2", cwd=root)
    assert result.code == 2, result.out + result.err
    assert "unable to open database file" in result.err


def test_an_error_after_the_tests_goes_with_the_results(project: Project, deflake: Deflake) -> None:
    root = project("django")
    (root / "project" / "runner.py").write_text(
        "from django.test.runner import DiscoverRunner\n\n\n"
        "class Runner(DiscoverRunner):\n"
        "    def teardown_databases(self, old_config, **kwargs):\n"
        "        super().teardown_databases(old_config, **kwargs)\n"
        '        raise RuntimeError("late")\n'
    )
    settings = root / "project" / "settings.py"
    settings.write_text(settings.read_text() + 'TEST_RUNNER = "project.runner.Runner"\n')
    result = deflake("shop/tests.py::ProductTest", "-n", "2", cwd=root)
    assert "errors outside of tests: failed 2 of 2 runs" in result.out, result.out + result.err
    assert "Django test run: RuntimeError: late" in result.out, result.out + result.err
    assert "ProductTest.test_creates: failed 0 of 2 runs (0%)" in result.out, result.out


def test_failed_system_checks_mean_the_tests_could_not_run(project: Project, deflake: Deflake) -> None:
    root = project("django")
    (root / "project" / "runner.py").write_text(
        "from django.core.management.base import SystemCheckError\n"
        "from django.test.runner import DiscoverRunner\n\n\n"
        "class Runner(DiscoverRunner):\n"
        "    def run_checks(self, databases):\n"
        '        raise SystemCheckError("misconfigured")\n'
    )
    settings = root / "project" / "settings.py"
    settings.write_text(settings.read_text() + 'TEST_RUNNER = "project.runner.Runner"\n')
    result = deflake("shop/tests.py::ProductTest", "-n", "2", cwd=root)
    assert result.code == 2, result.out + result.err
    assert "misconfigured" in result.err, result.out + result.err


def test_tests_of_a_file_with_a_database_test_run_on_the_test_database_in_any_set(
    project: Project,
    deflake: Deflake,
) -> None:
    root = project("django")
    (root / "shop" / "test_plain.py").write_text(
        "import unittest\n\nfrom django.test import TestCase\n\nfrom shop.models import Product\n\n\n"
        "class Plain(unittest.TestCase):\n"
        '    def test_creates(self):\n        Product.objects.create(name="left")\n\n'
        "    def test_counts(self):\n        self.assertEqual(Product.objects.count(), 0)\n\n\n"
        "class Database(TestCase):\n    def test_reads(self):\n        Product.objects.all()\n"
    )
    # The victim fails in half of the orders, so 20 runs miss it once in a million
    result = deflake("shop/test_plain.py", "-n", "20", cwd=root)
    # Run alone or with its culprit, the victim is still on the test database
    assert "culprit: Plain.test_creates" in result.out, result.out + result.err
    assert not (root / "db.sqlite3").exists(), result.out


def test_a_project_without_urls_still_has_its_activated_language_watched(
    project: Project,
    deflake: Deflake,
) -> None:
    root = project("django")
    settings = root / "project" / "settings.py"
    settings.write_text(settings.read_text().replace('ROOT_URLCONF = "project.urls"\n', ""))
    result = deflake("shop/test_leaks.py", "-n", "1", "-f", "json", cwd=root)
    leaks = {test["name"]: [leak["message"] for leak in test["leaks"]] for test in result.json}
    assert leaks["LeaksTest.test_activates_a_language"] == [
        "the active language was changed (activate() without deactivate()?)"
    ], result.err
