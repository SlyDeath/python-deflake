STATE = {"cleanup": True, "dirty": False}


def test_a_disables_cleanup():
    STATE["cleanup"] = False


def test_b_unrelated():
    assert 1 + 1 == 2


def test_c_pollutes():
    STATE["dirty"] = True


def test_d_cleans():
    if STATE["cleanup"]:
        STATE["dirty"] = False


def test_e_victim():
    assert not STATE["dirty"]
