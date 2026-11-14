import types
from typing import Any

import pytest

from deflake import events, pytest_plugin


def test_a_run_deflake_did_not_start_is_left_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in events.KEYS:
        monkeypatch.delenv(key, raising=False)

    # The plugin is installed for every pytest run of the project, not only for deflake's
    config: Any = object()
    assert pytest_plugin.pytest_cmdline_main(config) is None


def test_a_closure_variable_not_assigned_yet_is_empty() -> None:
    assert not pytest_plugin.cell_filled(types.CellType())
    assert pytest_plugin.cell_filled(types.CellType(1))


def test_a_function_without_source_is_at_its_line() -> None:
    assert pytest_plugin.source_file(len) is None
    assert pytest_plugin.lines_of(len, 7) == (7, 7)


def test_an_error_without_a_crash_location_is_the_error_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    try:
        raise ValueError("broken")
    except ValueError as error:
        # Only around the call, so pytest reports a failure of this test as usual
        with monkeypatch.context() as patched:
            patched.setattr(pytest.ExceptionInfo, "_getreprcrash", lambda self: None, raising=False)
            message = pytest_plugin.error_of(error)

    assert message == "ValueError: broken"
