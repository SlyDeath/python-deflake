from django.core.cache import cache

cache.set("feature", "old", version=1)


def test_new_version():
    assert cache.get("feature", version=2) is None
    cache.set("feature", "leaked", version=2)
    assert cache.get("feature", version=1) == "old"
    assert cache.get("feature", version=2) == "leaked"
