"""The end of a test process's output, where test runners say what went wrong, cleaned of what
changes from run to run, so one error stays one error"""

from __future__ import annotations

import re
from pathlib import Path

# How many lines an error shows
LINES = 15
# How much of the end of the output is read: a log of hundreds of megabytes stays on disk
KEEP = 256 * 1024
# Frames kept of each traceback, the last ones, nearest to the error
FRAMES = 3
# Whole lines the test runners print that change from run to run or say nothing about the error
NOISE = re.compile(
    r"^(?:={3,}.*={3,}|-{3,}|platform \S+ -- Python |rootdir: |configfile: |plugins: |cachedir: "
    r"|collected \d+ items?|collecting \.\.\.|Ran \d+ tests? in |OK(?: \(.*\))?$|FAILED \(.*\)$"
    r"|Found \d+ test\(s\)\.|System check identified |(?:Creating|Destroying|Cloning) test database "
    r"|Using existing test database |[.FEsxX]+$)"
)
# A seed inside a line, such as "Using --randomly-seed=41234"
SEED = re.compile(r"\b(seed[ =:]+)\d+", re.IGNORECASE)
# Addresses, such as of a thread in a faulthandler dump
ADDRESS = re.compile(r"0x[0-9a-fA-F]{6,16}")
# The first line of a frame in a Python traceback; the code line after it is indented further
FRAME = re.compile(r'^\s+File "[^"]*", line \d+')


def with_tail(message: str, output: str) -> str:
    """The message, and the end of the output when there is one"""
    end = lines(output)

    return f"{message}, its output ended with:\n" + "\n".join(end) if end else message


def read(log: str | Path) -> str:
    with Path(log).open("rb") as file:
        size = file.seek(0, 2)
        cut = size > KEEP
        file.seek(size - KEEP if cut else 0)
        text = file.read().decode("utf-8", "replace")

    return text.split("\n", 1)[1] if cut and "\n" in text else text


def lines(output: str) -> list[str]:
    kept = []
    for line in output.splitlines():
        line = line.rstrip()
        if not line.strip() or NOISE.match(line.strip()):
            continue
        kept.append(ADDRESS.sub("0x...", SEED.sub(r"\g<1>N", line)))

    return short_frames(kept)[-LINES:]


def short_frames(lines: list[str]) -> list[str]:
    """Keeps the last frames of every traceback: Python prints the nearest to the error last"""
    result: list[str] = []
    frames: list[list[str]] = []
    for line in [*lines, ""]:
        if FRAME.match(line):
            frames.append([line])
        elif frames and line.startswith("    ") and len(frames[-1]) == 1:
            frames[-1].append(line)
        else:
            if frames:
                if len(frames) > FRAMES:
                    result.append(f"  ... {len(frames) - FRAMES} more frames")
                result.extend(part for frame in frames[-FRAMES:] for part in frame)
                frames = []
            if line:
                result.append(line)

    return result
