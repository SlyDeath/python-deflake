import os


def test_a_pollutes():
    os.environ["CAUSE_FLAG"] = "leaked"
    os.environ["UNRELATED_FLAG"] = "noise"


def test_b_victim():
    assert os.getenv("CAUSE_FLAG") is None, "environment pollution"
