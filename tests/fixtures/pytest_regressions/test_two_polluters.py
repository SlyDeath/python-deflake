STATE = False


class TestPolluters:
    def test_a(self):
        global STATE
        STATE = True

    def test_b(self):
        global STATE
        STATE = True


class TestVictim:
    def test_victim(self):
        assert not STATE
