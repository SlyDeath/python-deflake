"""Time frozen or moved by freezegun or time-machine and never put back"""

from __future__ import annotations

import sys


class Clock:
    def snapshot(self) -> tuple[int, int]:
        freezegun = sys.modules.get("freezegun.api")
        time_machine = sys.modules.get("time_machine")
        frozen = len(getattr(freezegun, "freeze_factories", []) or [])
        # traveller_stack since time-machine 3, coordinates_stack before
        stack = getattr(time_machine, "traveller_stack", None) or getattr(
            time_machine,
            "coordinates_stack",
            None,
        )
        travelling = len(stack or [])

        return frozen, travelling

    def diff(self, before: tuple[int, int], after: tuple[int, int]) -> list[str]:
        messages = []
        if after[0] > before[0]:
            messages.append("freezegun is still freezing time (freeze_time().start() without stop()?)")

        if after[1] > before[1]:
            messages.append("time_machine is still travelling (travel().start() without stop()?)")

        return messages
