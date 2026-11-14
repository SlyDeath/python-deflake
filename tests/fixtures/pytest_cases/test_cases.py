import pytest


def test_always_fails():
    assert 1 == 2


@pytest.mark.skip(reason="never runs")
def test_skipped():
    pass


def test_passes():
    pass


@pytest.mark.parametrize("value", [1, 2])
def test_params(value):
    assert value
