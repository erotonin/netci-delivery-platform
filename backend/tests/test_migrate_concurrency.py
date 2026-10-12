"""Two migration runners against one empty database must both succeed.

A Kubernetes install applies the schema from a Job; a retried Job, two operators, or a
rollout that runs one migrator per replica all start runners side by side. Before the
advisory lock both read the same empty `schema_migrations`, both treated every file as
pending, and the second died mid-way on a relation or a primary key the first had just
created -- a failed rollout on a fresh cluster. Runs against a throwaway database created
for the test, because it needs one with no schema at all.
"""

from __future__ import annotations

import os
import threading
import uuid
from pathlib import Path
import importlib.util

import pytest

TEST_URL = os.getenv("NETCI_TEST_DATABASE_URL", "").strip()
pytestmark = pytest.mark.skipif(not TEST_URL, reason="NETCI_TEST_DATABASE_URL is not set")

ROOT = Path(__file__).resolve().parents[2]


def _load_migrate():
    spec = importlib.util.spec_from_file_location("netci_migrate", ROOT / "scripts" / "migrate.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def empty_database():
    import psycopg

    name = f"netci_migrate_race_{uuid.uuid4().hex[:10]}"
    admin_url = TEST_URL
    with psycopg.connect(admin_url, autocommit=True) as connection:
        connection.execute(f'CREATE DATABASE "{name}"')
    base, _, _ = admin_url.rpartition("/")
    try:
        yield f"{base}/{name}"
    finally:
        with psycopg.connect(admin_url, autocommit=True) as connection:
            connection.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s", (name,)
            )
            connection.execute(f'DROP DATABASE IF EXISTS "{name}"')


def test_two_runners_on_an_empty_database_both_finish_cleanly(empty_database, capsys):
    import psycopg

    migrate = _load_migrate()
    expected = len(migrate.migration_files())
    started = threading.Barrier(2)
    results: list[object] = []
    lock = threading.Lock()

    def runner() -> None:
        started.wait(timeout=10)
        try:
            outcome: object = migrate.run(empty_database, dry_run=False)
        except Exception as exc:  # noqa: BLE001 - the failure is the finding
            outcome = exc
        with lock:
            results.append(outcome)

    threads = [threading.Thread(target=runner) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120)

    assert results == [0, 0], results
    with psycopg.connect(empty_database) as connection:
        count = connection.execute("SELECT count(*) FROM schema_migrations").fetchone()[0]
    assert count == expected
