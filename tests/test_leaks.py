import json
import logging
import warnings
from typing import Any
from unittest import mock

import pytest

from deflake import leaks
from deflake.leaks import patches, states


def test_a_detector_that_breaks_reports_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    class Broken:
        def snapshot(self) -> dict[str, Any]:
            return {}

        def diff(self, before: Any, after: Any) -> list[str]:
            raise RuntimeError("broken")

    class Gone:
        def snapshot(self) -> None:
            return None

        def diff(self, before: Any, after: Any) -> list[str]:
            return ["never"]

    monkeypatch.setattr(leaks, "detectors", [Broken(), Gone()])
    assert leaks.since(leaks.snapshot()) == []
    # A detector that read nothing now says nothing either
    assert leaks.since([(Gone(), {})]) == []


def test_a_patch_whose_target_cannot_be_reached_is_named_by_its_attribute() -> None:
    class Patch:
        attribute = "value"

        def getter(self) -> object:
            raise ImportError("gone")

    assert patches.describe(Patch()) == "?.value"


def test_warnings_and_logger_settings_are_put_back_and_set_again(monkeypatch: pytest.MonkeyPatch) -> None:
    # A logger stays once made: this one has the defaults the other tests' snapshots expect
    logger = logging.getLogger("deflake.test.settings")
    monkeypatch.setattr(logger, "propagate", True)
    monkeypatch.setattr(logger, "disabled", False)
    process = states.Process()
    with warnings.catch_warnings():
        before = process.snapshot()
        warnings.simplefilter("error")
        logger.propagate = False
        logger.disabled = True
        after = process.snapshot()
        names = [
            "warnings.filters",
            "the propagate setting of logger 'deflake.test.settings'",
            "the disabled setting of logger 'deflake.test.settings'",
        ]
        assert set(names) <= set(process.changed(before, after))
        for name in names:
            process.restore(name, before[name])
        assert warnings.filters == before["warnings.filters"][0]
        assert (logger.propagate, logger.disabled) == (True, False)
        for name in names:
            process.restore(name, after[name])
        assert warnings.filters[0][0] == "error"
        assert (logger.propagate, logger.disabled) == (False, True)


def test_a_logger_made_by_the_culprit_is_put_back_to_the_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    logger = logging.getLogger("deflake.test.made")
    monkeypatch.setattr(logger, "propagate", False)
    states.Process().restore("the propagate setting of logger 'deflake.test.made'", None, exists=False)
    assert logger.propagate is True


def test_a_piece_of_the_process_that_was_there_before_has_no_default() -> None:
    with pytest.raises(ValueError, match=r"sys\.path did not exist before"):
        states.Process().default("sys.path")


def test_a_piece_of_the_process_that_is_not_known_is_an_error() -> None:
    with pytest.raises(ValueError, match="no state named the colour of the root logger"):
        states.Process().write("the colour of the root logger", None)


def test_a_patch_that_cannot_be_read_leaves_the_others() -> None:
    class Unreadable:
        attribute = "value"

        def getter(self) -> object:
            raise RuntimeError("gone")

    patcher = mock.patch("json.dumps", return_value="patched")
    try:
        patcher.start()
        # Ahead of the readable one, so reading goes on past it
        states.mock_patches().insert(0, Unreadable())
        assert list(states.Patched().read()) == ["mock.patch of json.dumps"]
    finally:
        active = states.mock_patches()
        active[:] = [patch for patch in active if not isinstance(patch, Unreadable)]
        patcher.stop()

    assert json.dumps(1) == "1"


def test_a_patch_that_is_not_active_cannot_be_put_back_as_missing() -> None:
    with pytest.raises(ValueError, match=r"mock\.patch of json\.loads is not active"):
        states.Patched().restore("mock.patch of json.loads", None, exists=False)
