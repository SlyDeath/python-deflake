"""deflake.django.leaks: the state of Django put back, each script in a process of its own"""

import subprocess
import sys
import textwrap

# Settings of a project, run ahead of each script
DJANGO = textwrap.dedent(
    """
    import django
    from django.conf import settings

    settings.configure(
        DATABASES={"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": "app.db"}},
        USE_I18N=True,
    )
    django.setup()
    """
)


def run_in_django(script: str) -> None:
    code = DJANGO + textwrap.dedent(script)
    run = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120)
    assert run.returncode == 0, run.stderr


def test_a_receiver_disconnected_and_not_connected_again_is_a_leak() -> None:
    run_in_django(
        """
        from django.test.signals import setting_changed

        from deflake.django import leaks


        def receiver(**kwargs):
            pass


        setting_changed.connect(receiver, weak=False)
        before = leaks.Receivers().snapshot()
        setting_changed.disconnect(receiver)
        (name,) = [name for name, signal in leaks.SIGNALS.items() if signal is setting_changed]
        message = f"a receiver of {name} was disconnected and not connected again"
        assert leaks.Receivers().diff(before, leaks.Receivers().snapshot()) == [message]
        """
    )


def test_no_active_language_is_written_as_none() -> None:
    run_in_django(
        """
        from django.utils import translation

        from deflake.django import leaks

        translation.activate("fr")
        state = leaks.State()
        state.write("the active language", None)
        # What deactivate_all() leaves, as a culprit that called it does
        assert translation.get_language() is None
        """
    )


def test_a_setting_made_by_the_culprit_is_put_back_to_missing() -> None:
    run_in_django(
        """
        from django.conf import settings

        from deflake.django import leaks

        settings.NEW = 1
        state = leaks.State()
        state.restore("settings.NEW", None, exists=False)
        assert not hasattr(settings, "NEW")
        """
    )


def test_a_piece_of_django_that_was_there_before_has_no_default() -> None:
    run_in_django(
        """
        from deflake.django import leaks

        try:
            leaks.State().default("the active language")
        except ValueError as error:
            assert str(error) == "the active language did not exist before", error
        else:
            raise AssertionError("no error")
        """
    )


def test_a_piece_django_does_not_have_is_an_error() -> None:
    run_in_django(
        """
        from deflake.django import leaks

        try:
            leaks.State().write("the colour", 1)
        except ValueError as error:
            assert str(error) == "no state named the colour", error
        else:
            raise AssertionError("no error")
        """
    )


def test_a_value_that_cannot_be_copied_is_kept_as_it_is() -> None:
    run_in_django(
        """
        import threading

        from deflake.django import leaks

        lock = threading.Lock()
        assert leaks.copied(lock) is lock
        """
    )
