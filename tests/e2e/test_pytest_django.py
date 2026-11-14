"""The deflake command on Django projects tested with pytest, with pytest-django and without it"""

from collections.abc import Callable
from pathlib import Path

from tests.e2e.conftest import Deflake

Project = Callable[[str], Path]


def test_a_cache_first_used_in_a_worker_leaks(project: Project, deflake: Deflake) -> None:
    root = project("pytest_django")
    settings = root / "project" / "settings.py"
    settings.write_text(
        settings.read_text() + '\nCACHES["external"] = {"BACKEND": "unavailable_backend.Cache"}\n'
    )
    result = deflake("test_thread_cache.py", "-n", "1", "-f", "json", cwd=root)
    assert result.code == 1, result.out + result.err
    test = next(test for test in result.json if test["name"] == "test_thread_cache")
    assert test["runs"]["failures"] == 0
    assert test["leaks"] == [{"message": "cache 'default' has new keys: feature", "runs": 1}]


def test_a_new_cache_key_version_is_a_leak(project: Project, deflake: Deflake) -> None:
    result = deflake("test_cache_versions.py", "-n", "1", "-f", "json", cwd=project("pytest_django"))
    assert result.code == 1, result.out + result.err
    test = next(test for test in result.json if test["name"] == "test_new_version")
    assert test["leaks"] == [{"message": "cache 'default' has new keys: :2:feature", "runs": 1}]


def test_tests_keep_the_order_of_their_kind(project: Project, deflake: Deflake) -> None:
    root = project("pytest_django")
    result = deflake("test_kinds.py", "-n", "4", cwd=root)
    assert result.code == 0, result.out + result.err
    orders = {tuple(path.read_text().split()) for path in root.glob("kinds-*")}
    assert orders == {("db", "db", "transactional", "plain", "plain")}


def test_rows_left_where_the_database_is_blocked(project: Project, deflake: Deflake) -> None:
    result = deflake("test_rows.py", "-n", "4", cwd=project("pytest_django"))
    assert result.out == (
        "test_flushed: failed 0 of 4 runs (0%)\n"
        "test_leaves_a_row: failed 0 of 4 runs (0%)\n"
        "  leaks: rows were created in shop_product (1) (4 of 4 runs)\n"
        "test_no_database: failed 0 of 4 runs (0%)\n"
        "test_rolled_back: failed 0 of 4 runs (0%)\n"
    ), result.out + result.err


def test_rows_a_wider_fixture_left_are_its_own(project: Project, deflake: Deflake) -> None:
    result = deflake("test_wide_rows.py", "-n", "2", cwd=project("pytest_django"))
    assert result.out == (
        "errors outside of tests: none in 2 runs, but leaks\n"
        "  leaks: fixture catalog (module scope), outside its tests: rows were created in shop_product (1)"
        " (2 of 2 runs)\n"
        "test_plain: failed 0 of 2 runs (0%)\n"
        "test_reads: failed 0 of 2 runs (0%)\n"
    ), result.out + result.err


def test_jobs_get_test_databases_of_their_own(project: Project, deflake: Deflake) -> None:
    root = project("pytest_django")
    settings = root / "project" / "settings.py"
    settings.write_text(
        settings.read_text().replace(
            '"NAME": "db.sqlite3"}',
            '"NAME": "db.sqlite3", "TEST": {"NAME": "test.sqlite3"}}',
        )
    )
    result = deflake("test_names.py", "-n", "4", "-j", "2", cwd=root)
    assert result.code == 0, result.out + result.err
    names = {path.read_text() for path in root.glob("name-*")}
    assert names <= {"test.sqlite3_1", "test.sqlite3_2"}, names
    assert names


def test_django_without_pytest_django_keeps_the_database(project: Project, deflake: Deflake) -> None:
    root = project("pytest_django")
    (root / "pytest.ini").write_text("[pytest]\naddopts = -p no:django\npythonpath = .\n")
    (root / "conftest.py").write_text(
        "import os\n\nimport django\n\n"
        'os.environ.setdefault("DJANGO_SETTINGS_MODULE", "project.settings")\ndjango.setup()\n'
    )
    (root / "test_cache.py").write_text(
        "from django.core.cache import cache\n\n\ndef test_cache():\n    cache.set('a', 1)\n"
    )
    result = deflake("test_cache.py", "-n", "2", cwd=root)
    assert result.out == (
        "test_cache: failed 0 of 2 runs (0%)\n  leaks: cache 'default' has new keys: a (2 of 2 runs)\n"
    ), result.out + result.err
    # The project's own database is never opened
    assert not (root / "db.sqlite3").exists()


def test_rows_of_the_test_that_sets_up_the_database(project: Project, deflake: Deflake) -> None:
    root = project("pytest_django")
    (root / "shop" / "migrations" / "0002_seed.py").write_text(
        "from django.db import migrations\n\n\ndef seed(apps, schema_editor):\n"
        "    apps.get_model('shop', 'Product').objects.create(name='seed')\n\n\n"
        "class Migration(migrations.Migration):\n    dependencies = [('shop', '0001_initial')]\n"
        "    operations = [migrations.RunPython(seed)]\n"
    )
    (root / "test_flush.py").write_text(
        "import pytest\n\n\n@pytest.mark.django_db(transaction=True)\ndef test_flushes():\n    pass\n"
    )
    result = deflake("test_flush.py", "-n", "2", cwd=root)
    # The flush after a transactional test takes away the rows of the data migration
    assert result.out == (
        "test_flushes: failed 0 of 2 runs (0%)\n"
        "  leaks: rows were deleted from shop_product (1) (2 of 2 runs)\n"
    ), result.out + result.err


def test_a_skipped_test_leaves_the_early_setup_to_the_next(project: Project, deflake: Deflake) -> None:
    root = project("pytest_django")
    (root / "shop" / "fixtures").mkdir()
    (root / "shop" / "fixtures" / "products.json").write_text(
        '[{"model": "shop.product", "pk": 1, "fields": {"name": "tea"}}]'
    )
    (root / "test_skip_first.py").write_text(
        "import unittest\n\nfrom django.test import TransactionTestCase\n\n\n"
        "class SkipTest(TransactionTestCase):\n    fixtures = ['products.json']\n\n"
        "    @unittest.skip('later')\n    def test_skipped(self):\n        pass\n\n"
        "    @unittest.skip('later')\n    def test_skipped_too(self):\n        pass\n\n"
        "    def test_one(self):\n        pass\n\n    def test_two(self):\n        pass\n"
    )
    result = deflake("test_skip_first.py", "-n", "8", cwd=root)
    assert "rows were" not in result.out, result.out
    assert result.code == 0, result.out + result.err


def test_storages_and_templates_used_for_the_first_time_are_not_leaks(
    project: Project,
    deflake: Deflake,
) -> None:
    root = project("pytest_django")
    settings = root / "project" / "settings.py"
    settings.write_text(
        settings.read_text()
        + 'STATIC_URL = "static/"\nMEDIA_ROOT = "media"\n'
        + 'TEMPLATES = [{"BACKEND": "django.template.backends.django.DjangoTemplates"}]\n'
    )
    (root / "shop" / "files.py").write_text(
        "from django.core.files.storage import FileSystemStorage\n\n"
        "photos = FileSystemStorage(location='photos')\n"
    )
    (root / "test_files.py").write_text(
        "from django.core.files.storage import default_storage\nfrom django.template import engines\n\n"
        "from shop.files import photos\n\n\n"
        "def test_static():\n"
        "    template = engines['django'].from_string(\"{% load static %}{% static 'app.css' %}\")\n"
        "    assert template.render() == '/static/app.css'\n\n\n"
        "def test_storages():\n"
        "    assert photos.path('a').endswith('a')\n"
        "    assert not default_storage.exists('nothing')\n\n\n"
        "def test_other():\n    pass\n"
    )
    result = deflake("test_files.py", "-n", "4", cwd=root)
    assert "leaks" not in result.out, result.out + result.err
    assert result.code == 0, result.out + result.err


def test_a_test_outside_a_class_takes_the_state_twice(project: Project, deflake: Deflake) -> None:
    root = project("pytest_django")
    (root / "conftest.py").write_text(
        "from pathlib import Path\n"
        "from deflake import leaks\n\n\n"
        "class Counter:\n"
        "    def snapshot(self):\n"
        "        with Path('snapshots').open('a') as file:\n"
        "            file.write('.')\n\n"
        "    def diff(self, before, after):\n"
        "        return []\n\n\n"
        "leaks.detectors.append(Counter())\n"
    )
    tests = "".join(f"\n\ndef test_{number}():\n    pass\n" for number in range(10))
    (root / "test_functions.py").write_text(tests)
    result = deflake("test_functions.py", "-n", "1", cwd=root)
    assert result.code == 0, result.out + result.err
    # Two for every test, before and after it; pytest-django's class fixture, set up anew for every
    # function outside a class, takes no more. The rest is for its session fixtures
    session = 6
    assert len((root / "snapshots").read_text()) <= 2 * 10 + session


def test_a_project_without_urls_still_has_its_activated_language_watched(
    project: Project,
    deflake: Deflake,
) -> None:
    root = project("pytest_django")
    settings = root / "project" / "settings.py"
    settings.write_text(settings.read_text().replace('ROOT_URLCONF = "project.urls"\n', ""))
    (root / "test_language.py").write_text(
        "from django.utils import translation\n\n\ndef test_activates():\n    translation.activate('de')\n"
    )
    result = deflake("test_language.py", "-n", "1", "-f", "json", cwd=root)
    test = next(test for test in result.json if test["name"] == "test_activates")
    assert [leak["message"] for leak in test["leaks"]] == [
        "the active language was changed (activate() without deactivate()?)"
    ], result.err
