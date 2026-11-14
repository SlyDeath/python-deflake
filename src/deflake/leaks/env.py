"""Environment variables set, changed or removed"""

from __future__ import annotations

import os

# Set by test runners themselves around each test
IGNORED = {"PYTEST_CURRENT_TEST"}


class Env:
    def snapshot(self) -> dict[str, str]:
        return {name: value for name, value in os.environ.items() if name not in IGNORED}

    @staticmethod
    def restore(name: str, value: str | None, *, exists: bool = True) -> None:
        if not exists or value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value

    @staticmethod
    def label(name: str) -> str:
        return f"os.environ[{name!r}]"

    @staticmethod
    def changed(before: dict[str, str], after: dict[str, str]) -> list[str]:
        return sorted(name for name in before.keys() | after.keys() if before.get(name) != after.get(name))

    def diff(self, before: dict[str, str], after: dict[str, str]) -> list[str]:
        messages = []
        for name in self.changed(before, after):
            if name not in before:
                messages.append(f"{self.label(name)} was set")
            elif name not in after:
                messages.append(f"{self.label(name)} was removed")
            elif before[name] != after[name]:
                messages.append(f"{self.label(name)} was changed")

        return messages
