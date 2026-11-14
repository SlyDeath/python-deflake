"""warnings.filters, which simplefilter and filterwarnings change for the whole process"""

from __future__ import annotations

import warnings
from collections.abc import Sequence
from typing import Any

CHANGED = "warnings.filters was changed (simplefilter without catch_warnings?)"


class WarningFilters:
    def snapshot(self) -> list[Any]:
        return filters_state(warnings.filters)

    def diff(self, before: list[Any], after: list[Any]) -> list[str]:
        return [] if before == after else [CHANGED]


def filters_state(filters: Sequence[Any]) -> list[Any]:
    return [
        (action, str(message), category, str(module), line)
        for action, message, category, module, line in filters
    ]
