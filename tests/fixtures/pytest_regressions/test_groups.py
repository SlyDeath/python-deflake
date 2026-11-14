import pytest

STATE = []


class TestPolluter:
    @pytest.fixture(scope="class", autouse=True)
    def pollutes(self):
        STATE.append("leaked")

    @pytest.mark.parametrize("value", [1, 2, 3], ids=["a::x", "b[::y]", "c::z"])
    def test_noop(self, value):
        pass


class TestVictim:
    def test_victim(self):
        assert STATE == []
