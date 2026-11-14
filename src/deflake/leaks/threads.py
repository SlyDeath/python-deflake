"""Threads a test started and left running"""

from __future__ import annotations

import re
import threading

# Threads and pools without a name of their own are numbered by how many were started before them
NUMBERED = re.compile(r"\d+")


class Threads:
    def snapshot(self) -> dict[int, str]:
        return {id(thread): NUMBERED.sub("N", thread.name) for thread in threading.enumerate()}

    def diff(self, before: dict[int, str], after: dict[int, str]) -> list[str]:
        return [f"thread {name!r} is still running" for key, name in after.items() if key not in before]
