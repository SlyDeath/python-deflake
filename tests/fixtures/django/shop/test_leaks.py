from django.conf import settings
from django.db.models.signals import post_save
from django.test import SimpleTestCase, override_settings
from django.utils import timezone, translation


def on_save(sender, **kwargs):
    pass


class LeaksTest(SimpleTestCase):
    def test_changes_a_setting(self):
        settings.DEBUG = True

    def test_leaves_settings_overridden(self):
        override_settings(USE_TZ=False).enable()

    def test_leaves_the_same_settings_overridden(self):
        override_settings(DEBUG=settings.DEBUG).enable()

    def test_overrides_settings_properly(self):
        with self.settings(USE_TZ=False):
            pass

    @override_settings(USE_TZ=False)
    def test_decorated(self):
        pass

    def test_activates_a_language(self):
        translation.activate("de")

    def test_activates_a_time_zone(self):
        timezone.activate("Europe/Berlin")

    def test_connects_a_receiver(self):
        post_save.connect(on_save)

    def test_clean(self):
        pass
