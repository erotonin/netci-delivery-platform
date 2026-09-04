"""Which store `build_database` is allowed to choose.

The in-memory store is a test adapter. Serving durable state from it outside local mode
would mean accepting an approval, answering 202 and losing it at the next deploy, so the
selection has to fail closed rather than degrade quietly.
"""

import pytest

from app.store import InMemoryDatabase, PostgresDatabase, build_database


def test_non_local_runtime_requires_a_durable_database(monkeypatch):
    monkeypatch.setenv("NETCI_ENVIRONMENT", "production")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL_FILE", raising=False)

    with pytest.raises(RuntimeError, match="DATABASE_URL"):
        build_database()


def test_local_runtime_may_use_the_explicit_in_memory_mode(monkeypatch):
    monkeypatch.setenv("NETCI_ENVIRONMENT", "local")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL_FILE", raising=False)

    assert isinstance(build_database(), InMemoryDatabase)


@pytest.mark.parametrize("environment", ["local", "production"])
def test_a_configured_database_url_always_selects_postgresql(monkeypatch, environment):
    monkeypatch.setenv("NETCI_ENVIRONMENT", environment)
    monkeypatch.setenv("DATABASE_URL", "postgresql://netci@127.0.0.1:5432/netci")
    monkeypatch.delenv("DATABASE_URL_FILE", raising=False)

    assert isinstance(build_database(), PostgresDatabase)


def test_health_never_reports_the_connection_string(monkeypatch):
    """An unreachable database must not put its credentials in an operator's screen."""

    monkeypatch.setenv("NETCI_ENVIRONMENT", "production")
    monkeypatch.setenv(
        "DATABASE_URL", "postgresql://netci:super-secret@127.0.0.1:1/netci"
    )
    database = build_database()

    assert database.describe() == "postgresql"
    assert "super-secret" not in database.describe()
