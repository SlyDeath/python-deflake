<div align="center">

# deflake

**Find flaky tests in pytest, unittest and Django and explain why they fail.**

Repeat · Isolate · Bisect · Detect leaked state

[![PyPI](https://img.shields.io/pypi/v/deflake-py?logo=pypi&logoColor=white&color=3775a9)](https://pypi.org/project/deflake-py/)
[![CI](https://github.com/SlyDeath/python-deflake/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/SlyDeath/python-deflake/actions/workflows/ci.yml)
[![Coverage](https://codecov.io/gh/SlyDeath/python-deflake/graph/badge.svg?branch=main)](https://codecov.io/gh/SlyDeath/python-deflake)
[![Python](https://img.shields.io/pypi/pyversions/deflake-py?logo=python&logoColor=white)](https://www.python.org)
[![License: MIT](https://img.shields.io/github/license/SlyDeath/python-deflake?color=blue)](LICENSE.txt)

</div>

A flaky test fails once in a while, someone adds a rerun, and the cause stays.
deflake runs a suspicious test many times and answers three questions:

1. **How often does it fail**, and with which errors and seeds?
2. **Does it fail on its own**, or only after some other test? If the latter, which one?
3. **What global state does a test leave behind**: environment variables, the clock, module globals, active `mock.patch`es, Django settings, caches and database rows?
4. **Is that leak what breaks the other test?** deflake puts it back and sets it again to prove it.

## Installation

```bash
pip install deflake-py
```

or add `deflake-py` to the development dependencies of your project. The package is `deflake-py`; the command and the module are `deflake`. It needs Python 3.11 or newer on Linux (with pidfd, kernel 5.3 or newer) or macOS, and has no dependencies of its own. It works with pytest 8 or newer, unittest and Django 5.2 or newer.

## Usage

Run deflake from the root of your project, where you run your tests (next to `manage.py` in a Django project):

```bash
deflake tests/test_orders.py --runs 50
deflake tests/test_orders.py::test_total --runs 50 --jobs 4
deflake tests/test_orders.py:42 --runs 50
deflake shop/tests.py::OrderTest --runs 20
deflake tests --runs 5
```

```
test_pollutes: failed 0 of 10 runs (0%)
  leaks: os.environ['FEATURE_FLAG'] was set (10 of 10 runs)
  leaks: test_order.STATE was changed (10 of 10 runs)
test_victim: failed 5 of 10 runs (50%)
  verdict: fails only after other tests (alone: failed 0 of 10 runs (0%))
  culprit: test_pollutes
  reproduce: DEFLAKE_SEED=40331 python -m pytest -p deflake.pytest_plugin test_order.py
  seeds 40331, 48172, 30602, 40073, 7643:
    assert not True
      at test_order.py:12
```

What happens:

1. **The whole file runs `--runs` times** (10 by default), every time in a fresh process with a new seed. The seed shuffles the classes and the tests inside them, but keeps every module and class together, so their fixtures, `setUpClass` and `setUpModule` still run once (pytest itself still regroups tests by the parameters of module and class fixtures, as it does without deflake). Before every test `random` is seeded with it, and so are Faker, factory_boy and NumPy when the tests use them. Running the whole file, not just one test, is what makes order-dependent failures show up. A test that takes its process down (`os._exit`, a crash, a signal) counts as failed in that run.
2. **Every test that failed runs again on its own**, as many times. The verdict is *fails on its own* if it still fails, which points at time, randomness or a race inside the test, or *fails only after other tests* if it does not. When it cannot run alone at all, as with errors outside of tests, the verdict is *failed, could not run it alone*.
3. **Order-dependent failures are bisected** with one of the failing seeds, down to a smallest set of tests that break the victim. deflake has the test runner run just those tests in the recorded order (pytest may regroup them by parametrized fixtures, as above) and watches only the victim, so other failing tests do not get in the way. A failure counts only when it is the failure seen in the runs of the file (the same error apart from numbers, or the same kind of error at the same line of the test file) and happens twice in a row, the set found must break the victim again at the end, and without each culprit the victim must complete every control run and either pass or produce a different failure, so a test that fails at random gets no culprits: the verdict is then *fails at random, not after particular tests*, with the command that runs that seed. When the failure came back with the same order but not with fewer tests, the report says so, with the command of that seed. When other tests break the victim just as well on their own, they are named too, after `or, just as well:`. Up to three failing seeds are tried. A culprit that two other tests of its class or module replace, because the state comes from a fixture they share, is named as such. A victim stopped by `--timeout` does not count as failing in a bisect, and a run that never got to the victim, because another test hung, is tried again.
4. **Every test is checked for leaks**, tests that skip themselves included: a snapshot of global state before the test, its fixtures and `setUp` included, and after their teardown. For a Django test that is before its transaction, fixtures and settings are set up and after they are rolled back, and for an `IsolatedAsyncioTestCase` after its event loop is closed. What a fixture wider than a test (`scope="module"`, `"class"`, `"package"` or `"session"`) or a `setUpClass` or `setUpModule` sets up and never puts back shows up under *errors outside of tests*, as `fixture NAME (module scope), outside its tests: ...`, not as a leak of the test that happened to run first.

A process that fails while every test in it passed, a file that cannot be collected or imported, or a failing `setUpClass`, is reported as *errors outside of tests*, with the end of its output. A run that breaks before its first test does not stop the others; only when no run gets to a test does deflake give up with exit code 2 and show how the test process ended.

`TEST_PATH::NAME` runs the whole file but reports only that test: all its parameters for a parametrized test, every test of a class for a class name. A unittest or Django test is named `Class.test_name`, and `::test_name` or `::Class` works too. `TEST_PATH:LINE` does the same for the test whose body holds the line, every test of the innermost class around the line, or else the test declared closest above it. A selected test that never started in any run makes deflake say so and exit with 1; the JSON report lists it with the verdict `not_run`.

`TEST_PATH` may be a folder: deflake then finds every `test_*.py`, `*_test.py` and `tests.py` in it and in its subfolders, leaving out virtual environments, `site-packages` and `node_modules`, and checks them one after another, each as if it were given on its own. The report of each file starts with `== FILE (3 of 40)`; with `--format json` that line goes to stderr as `deflake: checking FILE (3 of 40)`. A file that cannot run is reported and the others are still checked. At the end deflake names the files with failing or leaking tests and those it could not check, also after Ctrl-C, for the files checked so far. When the reader of the report stops early, as `head` does, deflake stops too, with exit code 141 if files were left unchecked.

### Options

| Option | Default | |
| --- | --- | --- |
| `-n`, `--runs N` | 10 | How many times to run each file |
| `-j`, `--jobs N` | 1 | How many test processes to run at once |
| `-t`, `--timeout SECONDS` | none | Stop a test process that runs longer; the test it was running counts as failed |
| `-f`, `--format FORMAT` | `text` | `text` or `json` |
| `-r`, `--runner RUNNER` | `auto` | `auto`, `pytest`, `django` or `unittest` |

Exit code: 0 when every test passed and nothing leaked (or every test skipped itself), 1 otherwise, 2 on usage errors, when the file has no tests or a filter of the project leaves them all out, or when the tests could not run (deflake then shows their output). For a folder it is the highest code of its files, and 2 when it has no test files. When interrupted with Ctrl-C, deflake interrupts its test processes, gives their tests up to 5 seconds to tear down what they set up, such as test databases, kills what is left and exits with 130 (a second Ctrl-C kills them at once), so a shell loop around it stops too. SIGTERM and SIGHUP, as from a CI timeout or a closed terminal, stop the test processes as well, with exit code 143 or 129.

### Test runners

With `--runner auto` deflake picks pytest when the project configures it (`pytest.toml`, `pytest.ini`, `[tool.pytest]` or `[tool.pytest.ini_options]` in `pyproject.toml`, `[pytest]` in `tox.ini`, `[tool:pytest]` in `setup.cfg`, or a `conftest.py` from the file up to the current folder) or when the file imports pytest or has test functions of its own. Otherwise it is Django's runner when there is a `manage.py` in the current folder, and unittest when there is not. A project with pytest-django goes to pytest, and Django's leak detectors work there too.

| Runner | Command | Reproduce |
| --- | --- | --- |
| pytest | `python -m pytest -p deflake.pytest_plugin FILE` | `DEFLAKE_SEED=N python -m pytest -p deflake.pytest_plugin FILE` |
| Django | `python manage.py test --testrunner deflake.django.Runner FILE` | `DEFLAKE_SEED=N python manage.py test --testrunner deflake.django.Runner FILE` |
| unittest | `python -m deflake.unittest_runner FILE` | `DEFLAKE_SEED=N python -m deflake.unittest_runner FILE` |

Run the `reproduce:` command as it is: `DEFLAKE_SEED` alone does nothing in a plain test run.

#### pytest

deflake runs pytest with the project's own configuration, and takes the order, the selection and the results into its own hands:

- Plugins that reorder or rerun tests are switched off in these processes: pytest-randomly, pytest-random-order, pytest-xdist, pytest-rerunfailures and flaky. Their options still parse, so `addopts = -n auto --reruns 3` keeps working; a `flaky` marker is accepted under `--strict-markers`.
- So are options that select, reorder or stop the tests on their own: `--lf`, `--ff`, `--nf`, `--sw`, `-x`, `--maxfail`, `--pdb` and `--trace`.
- pytest's cache goes to a folder of deflake's own, so `--lf` in your next run still remembers your last run, not deflake's.
- A test marked `flaky` is reported as such: its failures are hidden by reruns in the project's own runs, and a marked test that never fails here may not need the marker any more.
- pytest-timeout stays on and its timer is not reported as a leak. Its `signal` method fails the test as usual. Its `thread` method ends the whole process, so deflake reports the test as interrupted, sees no leaks after it and never takes such a run as evidence of a cause. deflake's own `--timeout` limits the whole test process.

#### Django

`deflake.django.Runner` is the project's own `TEST_RUNNER` with deflake on top, so what a custom runner sets up still happens. The file is taken as the module it is on the project's `sys.path`, as `manage.py` sets it up, so apps kept in a folder such as `apps/` are found. Django runs `TestCase`s first, then other database tests, then the rest; the seed shuffles each kind and keeps that order. `--parallel`, `--shuffle`, `--reverse`, `--failfast`, `--pdb` and `--debug-sql` are switched off, as deflake runs tests in its own order, one process per run.

#### unittest

`deflake.unittest_runner` imports the file as `python -m unittest FILE` does, as a module named after its path from the current folder, and runs its tests with unittest's own runner, `load_tests` included.

Every test process runs in a session of its own, without a terminal, as in CI: its input is empty and its output is not a TTY.

Processes left in its group are killed when the run ends. On Linux and macOS, processes started from the test interpreter through Python's `subprocess.Popen`, `os.fork` and `os.posix_spawn` are also registered and stopped even if they create another session, together with the processes of the group such a process leads, as a server started with `start_new_session=True` and its workers. Cleanup sends signals through Linux pidfds or macOS audit tokens, so PID reuse cannot redirect a signal to a replacement process. Group members are captured before the test process is reaped and signalled individually through these identities. A system without the required kernel API is rejected before tests start; cleanup never falls back to numeric PID or group signals. Forked Python children inherit this tracking until they exec another program. Programs that daemonize further after exec, out of that group, must be stopped by the test's teardown.

### Parallel runs and databases

With `--jobs` several processes run at once. Each gets `DEFLAKE_JOB` set to `1`, `2`, `3`... With Django's runner and with pytest-django every process gets test databases of its own: `test_myapp_1`, `test_myapp_2`... (an in-memory SQLite database is the process's own anyway). Tests that share another outside resource, such as a port, can do the same:

```python
# conftest.py
import os

DATABASE = f"myapp_test{os.environ.get('DEFLAKE_JOB', '')}"
```

The runs alone use `--jobs` and `DEFLAKE_JOB` as well. Bisect runs one process at a time, without `DEFLAKE_JOB`.

### Leak detectors

| Detector | Reports |
| --- | --- |
| Environment | Variables in `os.environ` set, changed or removed |
| Working directory | `os.chdir` not undone |
| Clock | freezegun or time-machine left on (`start()` without `stop()`) |
| Module globals | Global variables of the project's own modules assigned or changed in place; modules of installed packages are left out. Data is compared by value: numbers, strings, dates, paths and the like, lists, tuples, dicts and sets of them, objects of the standard library such as a counter, and objects of the project's own classes by their attributes, dataclasses, namespaces and pydantic models included. A global of more than 50,000 parts is compared as `pickle` dumps it, by the same rules, but with the order of its keys and of the items of its sets. An object of an installed library, such as a client, a logger, a QuerySet, a Faker or a lazy proxy, counts by identity: replacing it is a leak, what it sets up inside on its own use is not, and it is never asked to compute itself. Random generators and decimal contexts count by identity too, as they change on their own use. Python's own files count as installed even when its environment is inside the project folder, as with pixi or conda. Cached properties and methods an object of the project puts on itself on first use are left out |
| Patches | `mock.patch(...).start()` without `stop()` |
| sys | `sys.path`, the standard streams and the recursion limit |
| Logging | The level, handlers, `propagate` or `disabled` of a logger, new loggers included, and `logging.disable()` |
| Warning filters | `warnings.simplefilter` or `filterwarnings` outside of `catch_warnings` |
| Threads | Threads a test started and left running; numbers in their names, as in `Thread-3` or `ThreadPoolExecutor-0_1`, are left out, as they depend on the tests before |
| Signals | Signal handlers replaced and not put back |
| Process settings | `socket.setdefaulttimeout` and the `decimal` context |

With Django configured, by its test runner or by pytest-django, these are watched too:

| Detector | Reports |
| --- | --- |
| Settings | A setting assigned and not put back, and `override_settings(...).enable()` without `disable()` |
| Caches | New keys in a local memory cache (`LocMemCache`); other backends live outside of the test process |
| Activated | `translation.activate`, `timezone.activate` and `set_urlconf` without a matching deactivate |
| Receivers | Signal receivers of Django's own signals connected or disconnected and not put back; a file storage connecting itself on first use is Django's own doing |
| Rows | Rows created or deleted outside of a rolled back transaction, in every database, as a plain `unittest.TestCase` or a `TransactionTestCase` that flushes the data of migrations does. A test that may not use the database, such as a `SimpleTestCase`, is not checked. Only test databases are counted, never the project's own, and a mirror (`TEST['MIRROR']`) is counted with the database it mirrors |

A detector is any object with two methods: `snapshot()` returns the current state, or `None` when it does not apply, and `diff(before, after)` returns one message per leak. Add your own in `conftest.py`; detectors run only in the test processes deflake starts, so your regular test runs are not affected:

```python
from deflake import leaks


class FeatureFlagLeaks:
    def snapshot(self):
        return sorted(FeatureFlags.enabled())

    def diff(self, before, after):
        return [f"feature flag {flag} left on" for flag in sorted(set(after) - set(before))]


leaks.detectors.append(FeatureFlagLeaks())
```

The Module globals detector compares every global of the project before and after each test, and the Rows detector counts the rows of every table; if one of them is too slow for your project, leave it out, in `conftest.py` or in the test settings of a Django project:

```python
from deflake import leaks
from deflake.django.leaks import Rows
from deflake.leaks.modules import Modules

leaks.excluded += [Modules, Rows]
```

### Proving a leak is the cause

A leak next to a failure is a suspect, not a proof. For each culprit deflake takes every piece of state it leaked and runs an experiment in fresh processes with the same seed and order: two runs as they are, two that put the state back after the culprit, and two that put it back and set it again. A leak is confirmed as the cause only when the victim fails the same way in the runs as they are and when set again, and passes both times when the state is put back. Skips, timeouts, interrupted processes, errors outside of tests and other failures never confirm a cause.

```
test_victim: failed 5 of 10 runs (50%)
  verdict: fails only after other tests (alone: failed 0 of 10 runs (0%))
  culprit: test_pollutes
  environment cause: FEATURE_FLAG from test_order.py::test_pollutes: confirmed (seed 40331)
    baseline: same_failure, same_failure; repair: passed, passed; reapply: same_failure, same_failure
```

This works for:

- environment variables and plain values of the project's modules (`None`, bools, numbers, strings and bytes);
- `mock.patch` left started, set again with the culprit's own mock, and the keys a `mock.patch.dict` patched;
- time freezegun left frozen;
- the working directory, `sys.path`, the recursion limit, the socket timeout, the `decimal` context, signal handlers, warning filters and logging;
- under Django, the active language, time zone and URLconf, settings (an `override_settings` left enabled is tried setting by setting), individual added local memory cache entries (by alias and full stored key) and signal receivers.

Other leaks of a culprit, such as a list changed in place, rows of a database, a thread or time-machine, are shown under the victim as `not checked as the cause`: they are next to the failure, but not proven to cause it. Only what the report shows as the culprit's own leaks is tried, up to twenty per culprit; a change of a wider fixture set up with it is not the culprit's. A confirmed cause holds for the order tested; putting a value back cannot undo copies of it already made into other objects or services.

The JSON report adds `causes` to each test, with `culprit`, `key`, `seed`, `baseline`, `repair`, `reapply`, `detector` (`environment`, `module`, `patch`, `clock`, `process` or `django`) and `confirmed`, and `culprit_leaks` with every leak of the culprits and whether it was `checked` and `confirmed`. Values of variables are never included.

### JSON report

`--format json` prints one array for the whole run, with one object per test that ran or leaked (a test that skipped itself in every run and leaked nothing is left out, as in the text report) with `file`, `name`, `id` (the pytest node id), `verdict` (`stable`, `failed`, `fails_on_its_own`, `order_dependent`, `random`, `not_run` for a selected test that never started, or `not_checked` for a file of a folder that could not run, with its error under `errors`; `failed` means a test could not run alone, or that every run looking for its culprits broke before it), `runs` and `isolated_runs` counts (`isolated_runs` is `null` when the test never failed or could not run alone) (`total`, `failures`, `failure_rate` and `skipped`: `total` leaves out the runs where the test skipped itself), `errors` with their seeds, `leaks` (with `runs`, counting the runs where the test skipped itself too) and `culprits` (`ids`, `names`, `command`, `alone` when the test fails with no test before it, `group_wide`, `others` and `other_names` for further sets of tests that break it just as well on their own, `reproduced` when the failure came back with the same order but not with fewer tests, and `broken` when every run broke before the test) and `marked_flaky`. The format may change until 1.0.

## Tested on real projects

[Recorded cases](https://github.com/SlyDeath/python-deflake/blob/main/docs/real_world_cases.json) from SecureNative Python, Runway model-sdk and humanize, at pinned revisions: deflake finds the known flaky test and its cause before the published fix, and the test passes after it.

## Limitations

- deflake runs one file at a time, even when given a folder. A test broken by a test in another file shows up as failing only when the two files run together, which deflake does not do yet.
- A folder is searched by file name, not by your `python_files` setting.
- Leak detection sees what changed in the test process. It cannot see changes made by other processes, such as a browser or a server started by a test.

## Development

```bash
uv sync
uv run ruff check && uv run ruff format --check && uv run mypy && uv run pytest
```

## License

MIT, see [LICENSE.txt](https://github.com/SlyDeath/python-deflake/blob/main/LICENSE.txt).
