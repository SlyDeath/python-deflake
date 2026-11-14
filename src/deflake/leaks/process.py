"""State of the Python process itself: the working directory, sys, sockets, decimal, signals"""

from __future__ import annotations

import decimal
import signal
import socket
import sys
from pathlib import Path
from typing import Any

# Signals whose handlers a test may set, and must set back
SIGNALS = [
    getattr(signal, name)
    for name in ("SIGALRM", "SIGTERM", "SIGINT", "SIGHUP", "SIGUSR1", "SIGUSR2", "SIGCHLD", "SIGPIPE")
    if hasattr(signal, name)
]


class Cwd:
    def snapshot(self) -> str | tuple[str, int | None]:
        try:
            return str(Path.cwd())
        except OSError as error:
            # An unavailable cwd is observable state, not an inapplicable detector
            return ("unavailable", error.errno)

    def diff(self, before: str | tuple[str, int | None], after: str | tuple[str, int | None]) -> list[str]:
        return [] if before == after else ["the working directory was changed"]


class Sys:
    """sys.path, the standard streams, the recursion limit"""

    def snapshot(self) -> dict[str, Any]:
        return {
            "sys.path": list(sys.path),
            "sys.stdout": id(sys.stdout),
            "sys.stderr": id(sys.stderr),
            "sys.stdin": id(sys.stdin),
            "sys.getrecursionlimit()": sys.getrecursionlimit(),
        }

    def diff(self, before: dict[str, Any], after: dict[str, Any]) -> list[str]:
        return [f"{name} was changed" for name in before if before[name] != after.get(name)]


class Settings:
    """Process-wide settings of the standard library"""

    def snapshot(self) -> dict[str, Any]:
        return {
            "socket.getdefaulttimeout()": socket.getdefaulttimeout(),
            "decimal.getcontext()": context_state(decimal.getcontext()),
        }

    def diff(self, before: dict[str, Any], after: dict[str, Any]) -> list[str]:
        return [f"{name} was changed" for name in before if before[name] != after.get(name)]


def context_state(context: decimal.Context) -> tuple[Any, ...]:
    """What a decimal context computes with; its flags change with every operation"""
    return (
        context.prec,
        str(context.rounding),
        context.Emin,
        context.Emax,
        context.capitals,
        context.clamp,
        sorted(map(str, (flag for flag, on in context.traps.items() if on))),
    )


class Signals:
    def snapshot(self) -> dict[int, Any] | None:
        # Python can only read and set handlers in the main thread
        if not main_thread():
            return None

        return {number: signal.getsignal(number) for number in SIGNALS}

    def diff(self, before: dict[int, Any], after: dict[int, Any]) -> list[str]:
        return [
            handler_changed(number)
            for number in before
            if before[number] is not after.get(number) and before[number] != after.get(number)
        ]


def handler_changed(number: int) -> str:
    return f"the handler of {signal.Signals(number).name} was changed"


def main_thread() -> bool:
    import threading

    return threading.current_thread() is threading.main_thread()
