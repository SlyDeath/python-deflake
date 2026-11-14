"""unittest.mock patches started and never stopped"""

from __future__ import annotations

import os
import sys
from typing import Any


class Patches:
    def snapshot(self) -> dict[int, str] | None:
        mock = sys.modules.get("unittest.mock")
        active = getattr(getattr(mock, "_patch", None), "_active_patches", None)
        if active is None:
            return {}

        return {id(patch): describe(patch) for patch in active}

    def diff(self, before: dict[int, str], after: dict[int, str]) -> list[str]:
        return [
            f"mock.patch of {name} is still active (patch.start() without stop()?)"
            for key, name in after.items()
            if key not in before
        ]


def describe(patch: Any) -> str:
    if hasattr(patch, "in_dict"):
        return "os.environ" if patch.in_dict is os.environ else "a dict"

    try:
        target = patch.getter()
        owner = getattr(target, "__name__", type(target).__name__)
    except Exception:
        owner = "?"

    attribute = getattr(patch, "attribute", None)

    return f"{owner}.{attribute}" if attribute else owner
