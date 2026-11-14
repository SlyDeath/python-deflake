import os


def test_crashes():
    os._exit(3)
