import os
import pytest


def test_a_pollutes():
    os.environ["CAUSE_FLAG"] = "leaked"
    os.environ["CONTEXT_FLAG"] = "noise"


@pytest.mark.xfail(raises=ValueError)
def test_b_victim():
    if os.getenv("CAUSE_FLAG"):
        raise AssertionError("original failure")
    if os.getenv("CONTEXT_FLAG"):
        raise ValueError("expected failure after repair")
