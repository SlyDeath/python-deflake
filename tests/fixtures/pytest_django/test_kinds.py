import os
from pathlib import Path

import pytest


def record(kind):
    with Path(f"kinds-{os.getpid()}").open("a") as file:
        file.write(f"{kind}\n")


def test_plain_one():
    record("plain")


@pytest.mark.django_db
def test_db_one():
    record("db")


def test_plain_two():
    record("plain")


def test_db_two(db):
    record("db")


@pytest.mark.django_db(transaction=True)
def test_transactional():
    record("transactional")
