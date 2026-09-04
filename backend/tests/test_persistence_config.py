import pytest

from app.persistence import PostgresDeliveryStore, PostgresPortalStore


@pytest.mark.parametrize("store_type", [PostgresDeliveryStore, PostgresPortalStore])
def test_non_local_runtime_requires_durable_database(monkeypatch, store_type):
    monkeypatch.setenv("NETCI_ENVIRONMENT", "production")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL_FILE", raising=False)

    with pytest.raises(RuntimeError, match="DATABASE_URL"):
        store_type.from_env()


@pytest.mark.parametrize("store_type", [PostgresDeliveryStore, PostgresPortalStore])
def test_local_runtime_may_use_the_explicit_in_memory_mode(monkeypatch, store_type):
    monkeypatch.setenv("NETCI_ENVIRONMENT", "local")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL_FILE", raising=False)

    assert store_type.from_env() is None
