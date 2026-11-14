"""The logging module: levels, handlers and switches of loggers, and logging.disable()"""

from __future__ import annotations

import logging
from typing import Any


class Logging:
    def snapshot(self) -> dict[str, Any]:
        loggers: dict[str, Any] = {"": logging.getLogger()}
        loggers.update(
            (name, logger)
            for name, logger in logging.Logger.manager.loggerDict.items()
            if isinstance(logger, logging.Logger)
        )

        return {
            "disable": logging.root.manager.disable,
            "loggers": {
                name: (
                    logger.level,
                    [(id(handler), isinstance(handler, logging.NullHandler)) for handler in logger.handlers],
                    logger.propagate,
                    logger.disabled,
                )
                for name, logger in loggers.items()
            },
        }

    def diff(self, before: dict[str, Any], after: dict[str, Any]) -> list[str]:
        messages = []
        if after["disable"] != before["disable"]:
            messages.append(f"logging.disable() was left at level {after['disable']}")

        for name, new in after["loggers"].items():
            # A logger made during the test starts with the defaults
            old = before["loggers"].get(name, (logging.NOTSET, [], True, False))
            if name not in before["loggers"] and all(null for _, null in new[1]):
                # A library imported for the first time quiets its new logger as the logging docs ask
                old = (old[0], new[1], *old[2:])
            if new == old:
                continue
            label = f"logger {name!r}" if name else "the root logger"
            for index, part in enumerate(("level", "handlers", "propagate setting", "disabled setting")):
                if old[index] != new[index]:
                    messages.append(f"the {part} of {label} was changed")

        return messages
