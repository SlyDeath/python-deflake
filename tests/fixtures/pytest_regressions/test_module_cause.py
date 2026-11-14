import metadata_state


def test_a_pollutes():
    metadata_state.__file__ = None
    metadata_state.UNRELATED = "noise"


def test_b_victim():
    assert metadata_state.__file__ is not None, "module metadata pollution"
