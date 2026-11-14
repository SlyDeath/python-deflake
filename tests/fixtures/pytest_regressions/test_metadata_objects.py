import metadata_objects


def test_a_changes_metadata():
    metadata_objects.__file__ = None


def test_b_creates_object():
    metadata_objects.OBJ = metadata_objects.Item()


def test_c_mutates_object():
    metadata_objects.OBJ.value = 1
