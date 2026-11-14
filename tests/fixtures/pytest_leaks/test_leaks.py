import decimal
import logging
import os
import socket
import sys
import threading
import time
import warnings
from unittest import mock

import pytest

import settings


def test_changes_cwd(tmp_path):
    os.chdir(tmp_path)


def test_mutates_a_module_global():
    settings.FEATURES["beta"] = True


def test_assigns_a_module_global():
    settings.LIMIT = 99


def test_starts_a_patch():
    mock.patch("os.getpid", return_value=1).start()


def test_changes_sys_path():
    sys.path.insert(0, "/nowhere")


def test_changes_a_logger():
    logging.getLogger("fixture.logger").setLevel(logging.CRITICAL)


def test_imports_a_quiet_library():
    logging.getLogger("fixture.library").addHandler(logging.NullHandler())


def test_logs_to_a_new_handler():
    logging.getLogger("fixture.output").addHandler(logging.StreamHandler())


def test_disables_logging():
    logging.disable(logging.ERROR)


def test_changes_warning_filters():
    warnings.simplefilter("ignore")


def test_leaves_a_thread_running():
    stop = threading.Event()
    threading.Thread(target=stop.wait, name="fixture-worker", daemon=True).start()


def test_changes_socket_timeout():
    socket.setdefaulttimeout(3)


def test_changes_decimal_context():
    decimal.getcontext().prec = 5


def test_puts_everything_back(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("FIXTURE_VAR", "1")
    monkeypatch.setattr(settings, "LIMIT", 1)
    with mock.patch("os.getpid", return_value=1), warnings.catch_warnings():
        warnings.simplefilter("ignore")
        logging.getLogger("fixture.other").info("hi")


@pytest.fixture(scope="module")
def module_state():
    os.environ["FIXTURE_MODULE"] = "1"
    yield
    del os.environ["FIXTURE_MODULE"]


@pytest.fixture(scope="module")
def module_leak():
    os.environ["FIXTURE_MODULE_LEAK"] = "1"
    yield


def test_uses_module_state(module_state):
    assert os.environ["FIXTURE_MODULE"] == "1"


def test_uses_module_state_too(module_state):
    assert os.environ["FIXTURE_MODULE"] == "1"


def test_uses_a_leaking_fixture(module_leak):
    pass


def test_sleeps_without_leaking():
    time.sleep(0.01)


def test_travels_in_time():
    import time_machine

    time_machine.travel(0, tick=False).start()


def test_changes_a_signal_handler():
    import signal

    signal.signal(signal.SIGUSR1, lambda *args: None)
