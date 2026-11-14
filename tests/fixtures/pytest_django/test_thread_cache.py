from concurrent.futures import ThreadPoolExecutor

from django.core.cache import caches


def test_thread_cache():
    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(lambda: caches["default"].set("feature", "leaked", version=2)).result()

    with ThreadPoolExecutor(max_workers=1) as pool:
        assert pool.submit(lambda: caches["default"].get("feature", version=2)).result() == "leaked"

    assert getattr(caches._connections, "external", None) is None
