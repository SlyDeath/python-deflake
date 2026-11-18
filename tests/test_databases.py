import subprocess
import sys
import textwrap

# Django is configured in a process of its own, so the other tests never see its settings
SCRIPT = textwrap.dedent(
    """
    import django
    from django.conf import settings

    settings.configure(DATABASES={"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": "app.db"}})
    django.setup()

    from django.db import connections

    from deflake.django import databases

    databases.remember()
    assert databases.test_aliases() == [], "the project's own database"
    # What setting up a test database does on PostgreSQL and MySQL, whose test name is test_NAME
    connections["default"].settings_dict["NAME"] = "test_app.db"
    assert databases.test_aliases() == ["default"], "the test database"
    """
)


def test_a_database_switched_to_its_test_database_is_a_test_database() -> None:
    run = subprocess.run([sys.executable, "-c", SCRIPT], capture_output=True, text=True)
    assert run.returncode == 0, run.stderr


MIRROR = textwrap.dedent(
    """
    import django
    from django.conf import settings

    replica = {"ENGINE": "django.db.backends.sqlite3", "NAME": "app.db", "TEST": {"MIRROR": "default"}}
    settings.configure(
        DATABASES={"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": "app.db"}, "replica": replica}
    )
    django.setup()

    from django.db import connections

    from deflake.django import databases

    databases.remember()
    for alias in connections:
        connections[alias].settings_dict["NAME"] = "test_app.db"
    assert databases.test_aliases() == ["default"], databases.test_aliases()
    """
)


def test_a_mirror_is_counted_as_the_database_it_mirrors() -> None:
    run = subprocess.run([sys.executable, "-c", MIRROR], capture_output=True, text=True)
    assert run.returncode == 0, run.stderr


GONE = textwrap.dedent(
    """
    import gc

    import django
    from django.conf import settings

    settings.configure()
    django.setup()

    from django.core.signals import setting_changed

    from deflake.django.leaks import Receivers


    class Listener:
        def changed(self, **kwargs):
            pass


    listener = Listener()
    setting_changed.connect(listener.changed)
    detector = Receivers()
    before = detector.snapshot()
    del listener
    gc.collect()
    assert detector.diff(before, detector.snapshot()) == [], "an object gone is not a disconnection"
    """
)


def test_a_receiver_whose_object_is_gone_was_not_disconnected() -> None:
    run = subprocess.run([sys.executable, "-c", GONE], capture_output=True, text=True)
    assert run.returncode == 0, run.stderr


BROKEN = textwrap.dedent(
    """
    import django
    from django.conf import settings

    local = {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}
    settings.configure(CACHES={"broken": local, "default": local})
    django.setup()

    from django.core.cache import caches

    from deflake.django import leaks


    class Broken:
        # As a cache whose options its backend refuses
        def __getitem__(self, alias):
            if alias == "broken":
                raise ValueError("unknown option")

            return caches[alias]


    leaks.caches = Broken()
    caches["default"].set("probe", 1)
    state = leaks.State().read()
    assert "cache entry ('default', ':1:probe')" in state and "settings.CACHES" in state, sorted(state)
    """
)


def test_a_cache_that_cannot_be_made_leaves_the_rest_of_django_to_probe() -> None:
    run = subprocess.run([sys.executable, "-c", BROKEN], capture_output=True, text=True)
    assert run.returncode == 0, run.stderr


RECEIVERS = textwrap.dedent(
    """
    import django
    from django.conf import settings

    settings.configure(MEDIA_ROOT="media")
    django.setup()

    from django.core.files.storage import FileSystemStorage
    from django.test.signals import setting_changed

    from deflake.django import leaks

    calls = []


    def kept(**kwargs):
        calls.append("kept")


    def left(**kwargs):
        calls.append("left")


    setting_changed.connect(kept, weak=False)
    (name,) = [name for name, signal in leaks.SIGNALS.items() if signal is setting_changed]
    key = f"a receiver of {name}"
    state = leaks.State()
    before = state.read()[key]
    setting_changed.connect(left, weak=False)
    # A storage connects itself on first use, which is not the leak
    storage = FileSystemStorage()
    storage.base_location
    after = state.read()[key]
    assert before[1] != after[1]

    state.restore(key, before)
    setting_changed.send(sender=None, setting="MEDIA_ROOT", value="other", enter=True)
    assert calls == ["kept"], calls
    assert "base_location" not in storage.__dict__, "the storage still forgets its paths"

    state.restore(key, after)
    setting_changed.send(sender=None, setting="MEDIA_ROOT", value="media", enter=False)
    assert calls == ["kept", "kept", "left"], calls
    """
)


def test_receivers_are_put_back_and_connected_again() -> None:
    run = subprocess.run([sys.executable, "-c", RECEIVERS], capture_output=True, text=True)
    assert run.returncode == 0, run.stderr


SUFFIX = textwrap.dedent(
    """
    import django
    from django.conf import settings

    settings.configure(
        DATABASES={
            "default": {"ENGINE": "django.db.backends.dummy", "NAME": "shop"},
            "files": {"ENGINE": "django.db.backends.sqlite3", "NAME": "app.db", "TEST": {"NAME": "test.db"}},
        }
    )
    django.setup()

    from django.db import connections

    from deflake.django import databases

    databases.suffix("2")
    names = {alias: connections[alias].settings_dict["TEST"]["NAME"] for alias in connections}
    assert names == {"default": "test_shop_2", "files": "test.db_2"}, names
    """
)


def test_a_suffix_is_added_to_the_name_of_every_test_database() -> None:
    run = subprocess.run([sys.executable, "-c", SUFFIX], capture_output=True, text=True)
    assert run.returncode == 0, run.stderr
