from pathlib import Path

from deflake import tail

PYTEST_OUTPUT = """\
============================= test session starts ==============================
platform darwin -- Python 3.11.17, pytest-9.2.0, pluggy-1.6.0
rootdir: /tmp/project
configfile: pyproject.toml
plugins: django-4.14.0
collected 3 items

tests/test_crash.py ..
Fatal Python error: Segmentation fault

Current thread 0x00000001f0b2c840 (most recent call first):
  File "/tmp/project/tests/test_crash.py", line 9 in test_crash
"""


def test_runner_noise_is_left_out() -> None:
    assert tail.lines(PYTEST_OUTPUT) == [
        "tests/test_crash.py ..",
        "Fatal Python error: Segmentation fault",
        "Current thread 0x... (most recent call first):",
        '  File "/tmp/project/tests/test_crash.py", line 9 in test_crash',
    ]


def test_unittest_and_django_noise_is_left_out() -> None:
    output = """\
Found 4 test(s).
Creating test database for alias 'default'...
System check identified no issues (0 silenced).
..F.
----------------------------------------------------------------------
Ran 4 tests in 0.012s

FAILED (failures=1)
Destroying test database for alias 'default'...
deflake: seed 4242
"""
    assert tail.lines(output) == ["deflake: seed N"]


def test_long_tracebacks_keep_the_last_frames_and_the_error() -> None:
    frames = "".join(f'  File "/x/m{i}.py", line {i}, in f{i}\n    call{i}()\n' for i in range(10))
    output = f"Traceback (most recent call last):\n{frames}RuntimeError: boom\n"
    lines = tail.lines(output)
    assert lines[0] == "Traceback (most recent call last):"
    assert lines[1] == "  ... 7 more frames"
    assert lines[-1] == "RuntimeError: boom"
    assert len(lines) == 2 + 3 * 2 + 1


def test_only_the_last_lines_are_kept() -> None:
    output = "".join(f"line {i}\n" for i in range(40))
    assert tail.lines(output) == [f"line {i}" for i in range(40 - tail.LINES, 40)]


def test_with_adds_the_tail_to_a_message() -> None:
    assert tail.with_tail("the test process crashed", "") == "the test process crashed"
    assert tail.with_tail("crashed", "boom\n") == "crashed, its output ended with:\nboom"


def test_read_keeps_only_the_end_of_a_big_log(tmp_path: Path) -> None:
    log = tmp_path / "output.log"
    log.write_bytes(b"x" * (tail.KEEP + 100) + b"\nlast line\n")
    assert tail.read(log) == "last line\n"
