"""The file where a test process records one JSON line per event, and the variables that tell it
what to do. Both sides use this: the test process writes the file, deflake reads it.

Events:
  {"started": id, "name": name}           a test begins
  {"id": id, "name": name, "seed": seed,  a test ends: passed, failed or skipped
   "passed": bool, "skipped": bool, "error": str | None, "leaks": [str]}
  {"outside": str | None, "leaks": [str]} an error or leaks outside of any test
  {"listed": id, "name": name, "file": path, "line": int, "end": int}
                                          a test in the file, when listing was asked for

The sibling .processes file holds {"pid": int, "identity": str} records for descendant cleanup.
The identity is an OS birth identifier, not a value from the project
"""

from __future__ import annotations

import ctypes
import json
import sys
from pathlib import Path
from typing import Any

# Where a test process writes its events
OUTPUT_KEY = "DEFLAKE_OUTPUT"
# The seed that orders the tests
SEED_KEY = "DEFLAKE_SEED"
# A file with test ids to run in exactly this order instead of the whole file
ORDER_KEY = "DEFLAKE_ORDER"
# A file with test ids to run, in the order the seed gives them
ONLY_KEY = "DEFLAKE_ONLY"
# Set to list the tests of the file instead of running them
LIST_KEY = "DEFLAKE_LIST"
# JSON describing an environment experiment: culprit id, mode and optional key; never values
PROBE_KEY = "DEFLAKE_PROBE"
# Every variable that tells a test process what to do: a test that starts deflake itself must not
# pass them on
KEYS = (OUTPUT_KEY, SEED_KEY, ORDER_KEY, ONLY_KEY, LIST_KEY, PROBE_KEY)
# The number of the process among those running at once with --jobs. The project's tests read it
# too, so a test process keeps it, and deflake started by a test does not pass it on
JOB_KEY = "DEFLAKE_JOB"


def process_file(output: str | Path) -> Path:
    """The append-only registry of processes started by this run, separate from test results"""
    return Path(output).with_suffix(".processes")


def process_identity(pid: int) -> str | None:
    """An OS birth identifier: a reused PID must never authorize killing another process"""
    try:
        if sys.platform == "darwin":
            data = ctypes.create_string_buffer(136)
            library = ctypes.CDLL("/usr/lib/libproc.dylib")
            library.proc_pidinfo.argtypes = [
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_uint64,
                ctypes.c_void_p,
                ctypes.c_int,
            ]
            if library.proc_pidinfo(pid, 3, 0, data, len(data)) == len(data):
                return data.raw[120:136].hex()
        elif sys.platform.startswith("linux"):
            # Field 22 follows a comm field that can itself contain spaces and parentheses
            state = Path(f"/proc/{pid}/stat").read_text()
            return state.rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError):
        return None

    return None


def start(path: str | Path) -> None:
    """Creates the file, so a run where no test wrote a line still tells the process got going"""
    Path(path).touch()


def append(path: str | Path, **fields: Any) -> None:
    line = json.dumps(clean(fields), ensure_ascii=False)
    with Path(path).open("a", encoding="utf-8") as file:
        file.write(line + "\n")


def write_ids(path: str | Path, ids: list[str]) -> None:
    Path(path).write_text(json.dumps(ids), encoding="utf-8")


def read_ids(path: str | Path) -> list[str]:
    ids = json.loads(Path(path).read_text(encoding="utf-8"))

    return [str(id) for id in ids]


def clean(value: Any) -> Any:
    """Error messages can hold lone surrogates from undecodable bytes, which UTF-8 cannot encode"""
    if isinstance(value, str):
        return value.encode("utf-8", "replace").decode("utf-8")
    if isinstance(value, (list, tuple)):
        return [clean(item) for item in value]
    if isinstance(value, dict):
        return {str(key): clean(item) for key, item in value.items()}

    return value


def audit_token(pid: int) -> bytes | None:
    """Acquire an audit token from a Mach task name port, releasing the port afterwards"""
    library = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
    own_task = ctypes.c_uint.in_dll(library, "mach_task_self_").value
    port = ctypes.c_uint()
    if library.task_name_for_pid(own_task, pid, ctypes.byref(port)) != 0:
        return None

    try:
        token = (ctypes.c_uint * 8)()
        count = ctypes.c_uint(8)
        if library.task_info(port, 15, ctypes.byref(token), ctypes.byref(count)) == 0:
            return bytes(token)
    finally:
        library.mach_port_deallocate(own_task, port)

    return None
