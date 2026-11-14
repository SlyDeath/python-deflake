"""The test databases of a test process deflake started"""

from __future__ import annotations

import contextlib
from collections.abc import Generator
from typing import Any

from django.db import connections

# pytest-django's django_db_blocker, which blocks the database outside of tests that ask for it
blocker: Any = None


def suffix(job: str) -> None:
    """Every process of deflake --jobs gets test databases of its own"""
    for alias in connections:
        config = connections[alias].settings_dict
        test = config.setdefault("TEST", {})
        if config["ENGINE"].endswith("sqlite3"):
            # An in-memory database is the process's own anyway
            if test.get("NAME") and test["NAME"] != ":memory:" and "mode=memory" not in test["NAME"]:
                test["NAME"] = f"{test['NAME']}_{job}"
            continue
        test["NAME"] = f"{test.get('NAME') or 'test_' + config['NAME']}_{job}"


# The names of the project's own databases, taken before the test databases are set up
originals: dict[str, Any] = {}


def remember() -> None:
    for alias in connections:
        with contextlib.suppress(Exception):
            originals.setdefault(alias, connections[alias].settings_dict["NAME"])


def test_aliases() -> list[str]:
    """Databases already switched to their test database. The others are the project's own and are
    never touched: before the test databases are set up, or with Django set up without them"""
    aliases = []
    seen = set()
    for alias in connections:
        connection = connections[alias]
        with contextlib.suppress(Exception):
            config = connection.settings_dict
            # A mirror, or another alias of the same database, has its rows counted once
            where = (config["ENGINE"], config.get("HOST"), config.get("PORT"), config["NAME"])
            if config.get("TEST", {}).get("MIRROR") or where in seen:
                continue
            name = config["NAME"]
            # Setting up a test database puts its name in NAME; the name it is given is test_NAME on
            # most backends, which cannot be told from NAME afterwards
            switched = alias in originals and name != originals[alias]
            if switched or name == connection.creation._get_test_db_name():
                seen.add(where)
                aliases.append(alias)

    return aliases


@contextlib.contextmanager
def access() -> Generator[None, None, None]:
    """Lets deflake count rows where pytest-django blocks the database"""
    if blocker is None:
        yield
        return

    with blocker.unblock():
        yield
