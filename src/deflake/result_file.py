"""Reads the events a test process wrote (see events)"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

Row = dict[str, Any]


def read(path: str | Path) -> list[Row]:
    """One dict per line. A process killed in the middle of a write leaves a broken last line,
    which is skipped"""
    rows = []
    with Path(path).open(encoding="utf-8", errors="replace") as file:
        for line in file:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                rows.append(row)

    return rows
