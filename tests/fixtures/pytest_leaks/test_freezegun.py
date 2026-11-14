import freezegun


def test_freezes_time():
    freezegun.freeze_time("2020-01-01").start()


def test_does_not_freeze():
    pass
