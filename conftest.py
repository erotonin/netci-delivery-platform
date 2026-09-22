"""Guards that apply to every test run, before `app.main` is imported anywhere.

`app.main` builds its database at import time from `DATABASE_URL`. The suite's own knob
is `NETCI_TEST_DATABASE_URL`, and its fixtures TRUNCATE that database. When both are set
to different places, the tests read one database and the application under test writes
another, which surfaces as a couple of hundred unrelated errors rather than as the
configuration mistake it is.

The worse case is the one this repository has already lived through: `DATABASE_URL`
still pointing at the live stack's database while pytest runs. The application under
test then writes into live data for the length of the run. Refusing costs one sentence;
the alternative cost every live run, deployment and module on this host once already.
"""

import os

import pytest


def pytest_configure(config: pytest.Config) -> None:
    runtime_url = os.environ.get("DATABASE_URL", "").strip()
    if not runtime_url:
        return

    test_url = os.environ.get("NETCI_TEST_DATABASE_URL", "").strip()
    same = " (the same one)" if runtime_url == test_url else ""
    raise pytest.UsageError(
        f"DATABASE_URL is set{same}; unset it for the test run.\n"
        f"  DATABASE_URL            = {runtime_url}\n"
        f"  NETCI_TEST_DATABASE_URL = {test_url or '(unset)'}\n"
        "`app.main` builds its store from DATABASE_URL at import, so the API under test "
        "stops using the in-memory store the suite expects -- including when both point "
        "at the same database, which fails as a few hundred unrelated errors. The tests "
        "that want PostgreSQL set it themselves, per test. Run `unset DATABASE_URL`. "
        "Never point either variable at the live stack\'s database."
    )
