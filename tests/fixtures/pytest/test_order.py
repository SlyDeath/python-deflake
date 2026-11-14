import os

STATE = {"polluted": False}


def test_pollutes():
    STATE["polluted"] = True
    os.environ["DEFLAKE_FIXTURE_FLAG"] = "1"


def test_victim():
    assert not STATE["polluted"]


def test_unrelated():
    assert 1 + 1 == 2


class TestGroup:
    def test_one(self):
        pass

    def test_two(self):
        pass
