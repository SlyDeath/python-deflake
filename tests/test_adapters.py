import importlib.util
from pathlib import Path

import pytest

from deflake import adapters
from deflake.adapters import Error


def test_a_runner_that_is_not_installed_is_an_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(importlib.util, "find_spec", lambda *args, **kwargs: None)
    with pytest.raises(Error, match="pytest is not installed"):
        adapters.for_path("test_x.py", "pytest")

    (tmp_path / "manage.py").write_text("")
    with pytest.raises(Error, match="Django is not installed"):
        adapters.for_path("test_x.py", "django")


def test_the_django_runner_needs_manage_py(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    with pytest.raises(Error, match=r"needs manage\.py"):
        adapters.for_path("test_x.py", "django")


def test_an_unknown_runner_is_an_error() -> None:
    with pytest.raises(Error, match="unknown runner: nose"):
        adapters.for_path("test_x.py", "nose")


def test_a_file_that_cannot_be_read_reads_as_empty(tmp_path: Path) -> None:
    assert adapters.read(tmp_path / "missing.py") == ""


def test_line_fallback_scans_for_the_nearest_line_once(monkeypatch):
    from deflake.adapters import pytest as adapter

    scans = []

    def maximum(lines):
        scans.append(len(lines))

        return max(lines)

    monkeypatch.setattr(adapter, "max", maximum, raising=False)
    rows = [{"file": __file__, "listed": i, "line": 2, "end": 2, "groups": []} for i in range(10)]
    assert adapter.by_line(__file__, 3, rows) == list(range(10))
    assert len(scans) <= 1
