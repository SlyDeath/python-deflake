"""Runs Django tests for deflake: python manage.py test --testrunner deflake.django.Runner LABEL.

Runner is the project's own TEST_RUNNER with deflake's order, recording and leak detection on top,
so what the project's runner sets up still happens
"""

from __future__ import annotations

from typing import Any


def __getattr__(name: str) -> Any:
    # Built on first use: the project's runner is known only once the settings are
    if name == "Runner":
        from deflake.django.runner import runner_class

        return runner_class()

    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
