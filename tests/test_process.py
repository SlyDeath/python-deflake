import os

from deflake import leaks
from deflake.leaks.process import Cwd


def test_deleted_cwd_is_observable(tmp_path):
    original = os.open(".", os.O_RDONLY)
    detector = Cwd()
    before: leaks.Snapshot = [(detector, detector.snapshot())]
    directory = tmp_path / "deleted"
    directory.mkdir()
    try:
        os.chdir(directory)
        directory.rmdir()
        assert leaks.since(before) == ["the working directory was changed"]
    finally:
        os.fchdir(original)
        os.close(original)


def test_deleted_cwd_can_be_compared_and_restored(tmp_path):
    original = os.open(".", os.O_RDONLY)
    detector = Cwd()
    directory = tmp_path / "deleted"
    directory.mkdir()
    try:
        os.chdir(directory)
        directory.rmdir()
        before = detector.snapshot()
        assert detector.diff(before, detector.snapshot()) == []
        os.fchdir(original)
        assert detector.diff(before, detector.snapshot()) == ["the working directory was changed"]
    finally:
        os.fchdir(original)
        os.close(original)
