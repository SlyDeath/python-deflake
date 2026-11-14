# Changelog

## [0.1.0]

First public release.

- `deflake TEST_PATH[:LINE|::NAME]` runs a test file many times, each in a fresh process with a new seed, and reports how often every test failed, with errors grouped by message and the seeds that reproduce them. A folder checks every test file in it.
- Runs pytest, Django's `manage.py test` (on top of the project's own `TEST_RUNNER`) and unittest; the runner is picked from the project, or with `--runner`.
- Every failing test is run again on its own, which tells a test that fails on its own from one that fails only after other tests.
- Bisect finds the tests that break an order-dependent test, names every set of them that does it on its own, and points at a fixture or hook of a group when any of its tests would do. A test that fails at random gets no culprits.
- Leaks of global state: environment variables, the working directory, freezegun and time-machine, module globals, `mock.patch`, `sys`, logging, warning filters, threads, signal handlers, the socket timeout and the `decimal` context; with Django also settings, local memory caches, the active language, time zone and URLconf, signal receivers and database rows. Leaks of fixtures wider than a test and of `setUpClass` are reported apart from the tests.
- A leak next to a failure is proven its cause: deflake puts the state back after the culprit, sees the test pass, sets it again and sees the test fail. This works for environment variables, module values, `mock.patch`, freezegun, the working directory, `sys.path`, the recursion limit, the socket timeout, the decimal context, signal handlers, warning filters and logging, and under Django for the active language, time zone, URLconf, settings, local memory caches and signal receivers. Other leaks of a culprit are shown under the failing test as not checked as the cause.
- `random`, Faker, factory_boy and NumPy are seeded before every test.
- `--jobs` runs test processes at once, with test databases of their own under Django and pytest-django; `--timeout` stops a hanging one; `--format json` gives a report for tools.
- An interrupt lets the tests tear down what they set up, as test databases, before their processes are stopped; a second interrupt stops them at once. Processes the tests start, servers in a session of their own included, are stopped with them.
- Plugins that reorder or rerun tests (pytest-randomly, pytest-random-order, pytest-xdist, pytest-rerunfailures, flaky) are switched off in the test processes deflake starts, and tests marked `flaky` are pointed out.
