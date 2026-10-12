"""Tests for Service Catalog and Dependency Graph."""

import pytest
from app.catalog.services import CatalogServiceManager, CatalogValidationError
from app.store.memory import InMemoryDatabase


@pytest.fixture()
def catalog_mgr():
    db = InMemoryDatabase()
    with db.transaction() as session:
        yield CatalogServiceManager(session)


def test_register_and_get_service(catalog_mgr: CatalogServiceManager):
    svc = catalog_mgr.register_service(
        service_id="svc-auth",
        name="Authentication Service",
        owning_team="sec-team",
        tier="tier-0",
        lifecycle="active",
        repo_url="https://github.com/org/auth",
        docs_url="https://docs.org/auth",
        metadata={"criticality": "high"},
    )
    assert svc.id == "svc-auth"
    assert svc.owning_team == "sec-team"
    assert svc.tier == "tier-0"


def test_register_invalid_tier_or_lifecycle(catalog_mgr: CatalogServiceManager):
    with pytest.raises(CatalogValidationError, match="invalid tier"):
        catalog_mgr.register_service(
            service_id="svc-bad",
            name="Bad Tier",
            owning_team="team-a",
            tier="tier-99",
        )

    with pytest.raises(CatalogValidationError, match="invalid lifecycle"):
        catalog_mgr.register_service(
            service_id="svc-bad",
            name="Bad Lifecycle",
            owning_team="team-a",
            lifecycle="unknown_status",
        )


def test_update_service(catalog_mgr: CatalogServiceManager):
    catalog_mgr.register_service(
        service_id="svc-billing",
        name="Billing API",
        owning_team="billing-team",
    )

    updated = catalog_mgr.update_service(
        service_id="svc-billing",
        tier="tier-1",
        lifecycle="deprecated",
        description="Legacy billing engine",
    )
    assert updated.tier == "tier-1"
    assert updated.lifecycle == "deprecated"
    assert updated.description == "Legacy billing engine"


def test_dependency_graph_and_cycle_detection(catalog_mgr: CatalogServiceManager):
    catalog_mgr.register_service(service_id="svc-a", name="Service A", owning_team="team-a")
    catalog_mgr.register_service(service_id="svc-b", name="Service B", owning_team="team-b")
    catalog_mgr.register_service(service_id="svc-c", name="Service C", owning_team="team-c")

    # A -> B -> C
    catalog_mgr.add_dependency(source_service_id="svc-a", target_service_id="svc-b", dependency_type="sync")
    catalog_mgr.add_dependency(source_service_id="svc-b", target_service_id="svc-c", dependency_type="async")

    graph_a = catalog_mgr.get_dependency_graph("svc-a")
    assert graph_a.upstream == ["svc-b"]
    assert not graph_a.has_cycle
    assert len(graph_a.nodes) == 3

    # Add cycle C -> A
    catalog_mgr.add_dependency(source_service_id="svc-c", target_service_id="svc-a", dependency_type="sync")
    graph_cycle = catalog_mgr.get_dependency_graph("svc-a")
    assert graph_cycle.has_cycle is True
    assert len(graph_cycle.cycles) > 0

    # Disallow self-dependency
    with pytest.raises(CatalogValidationError, match="cannot depend on itself"):
        catalog_mgr.add_dependency(source_service_id="svc-a", target_service_id="svc-a")
