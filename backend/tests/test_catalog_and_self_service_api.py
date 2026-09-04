"""Integration tests for Phase 12: Catalog, Templates, Previews, and Self-Service APIs."""

from uuid import uuid4
import pytest
from fastapi.testclient import TestClient
from app.main import app
from app.store.memory import InMemoryDatabase


@pytest.fixture()
def client():
    from app.main import database, app
    if hasattr(database, "clear"):
        database.clear()
    return TestClient(app)


def test_catalog_services_api_lifecycle(client: TestClient):
    # 1. Register service
    resp = client.post(
        "/catalog/services",
        headers={"X-Forwarded-User": "developer-alice"},
        json={
            "serviceId": "svc-order-api",
            "name": "Order Management API",
            "description": "Handles retail orders and cart checkouts",
            "owningTeam": "checkout-eng",
            "tier": "tier-1",
            "lifecycle": "active",
            "repoUrl": "https://github.com/org/order-api",
            "docsUrl": "https://docs.org/order-api",
            "metadata": {"criticality": "high"},
        },
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["serviceId"] == "svc-order-api"
    assert data["owningTeam"] == "checkout-eng"

    # Register target service for dependency
    client.post(
        "/catalog/services",
        headers={"X-Forwarded-User": "developer-alice"},
        json={
            "serviceId": "svc-inventory-api",
            "name": "Inventory API",
            "owningTeam": "warehouse-eng",
            "tier": "tier-1",
        },
    )

    # 2. List services
    list_resp = client.get("/catalog/services?owningTeam=checkout-eng")
    assert list_resp.status_code == 200
    items = list_resp.json()["items"]
    assert len(items) == 1
    assert items[0]["serviceId"] == "svc-order-api"

    # 3. Get service
    get_resp = client.get("/catalog/services/svc-order-api")
    assert get_resp.status_code == 200
    assert get_resp.json()["name"] == "Order Management API"

    # 4. Update service
    put_resp = client.put(
        "/catalog/services/svc-order-api",
        headers={"X-Forwarded-User": "developer-alice"},
        json={"tier": "tier-0", "lifecycle": "active"},
    )
    assert put_resp.status_code == 200
    assert put_resp.json()["tier"] == "tier-0"

    # 5. Add dependency
    dep_resp = client.post(
        "/catalog/services/svc-order-api/dependencies",
        headers={"X-Forwarded-User": "developer-alice"},
        json={
            "targetServiceId": "svc-inventory-api",
            "dependencyType": "sync",
            "description": "Stock checking during checkout",
        },
    )
    assert dep_resp.status_code == 201

    # 6. Get dependencies graph
    graph_resp = client.get("/catalog/services/svc-order-api/dependencies")
    assert graph_resp.status_code == 200
    graph_data = graph_resp.json()
    assert graph_data["upstream"] == ["svc-inventory-api"]
    assert graph_data["hasCycle"] is False


def test_catalog_templates_api(client: TestClient):
    # 1. List templates (auto-seeds builtins)
    resp = client.get("/catalog/templates")
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) >= 3

    # 2. Get specific template
    tmpl_resp = client.get("/catalog/templates/fastapi-service")
    assert tmpl_resp.status_code == 200
    assert tmpl_resp.json()["templateId"] == "fastapi-service"

    # 3. Instantiate template
    inst_resp = client.post(
        "/catalog/templates/fastapi-service/instantiate",
        headers={"X-Forwarded-User": "developer-bob"},
        json={
            "applicationName": "loyalty-api",
            "owningTeam": "loyalty-team",
            "parameters": {"port": 8080, "python_version": "3.12"},
        },
    )
    assert inst_resp.status_code == 200
    inst_data = inst_resp.json()
    assert inst_data["applicationName"] == "loyalty-api"
    assert inst_data["deploymentConfig"]["port"] == 8080


def test_preview_environments_api(client: TestClient):
    # Create application first
    app_resp = client.post(
        "/applications",
        headers={"X-Forwarded-User": "developer-alice", "Idempotency-Key": f"app-key-{uuid4()}"},
        json={
            "name": "cart-api",
            "repositoryUrl": "https://github.com/org/cart-api",
            "pipelineTemplate": "container-ci-cd-v1",
            "runtime": "docker",
            "defaultEnvironment": "dev",
            "stages": [],
        },
    )
    assert app_resp.status_code == 201
    app_id = app_resp.json()["id"]

    # 1. Create preview environment
    create_resp = client.post(
        "/preview-environments",
        headers={"X-Forwarded-User": "developer-alice"},
        json={
            "applicationId": app_id,
            "pullRequestId": "PR-55",
            "commitSha": "e" * 40,
            "ttlSeconds": 7200,
        },
    )
    assert create_resp.status_code == 201
    prv_data = create_resp.json()
    prv_id = prv_data["previewId"]
    assert prv_data["status"] == "active"
    assert "pr-55" in prv_id

    # 2. List preview environments
    list_resp = client.get(f"/preview-environments?applicationId={app_id}")
    assert list_resp.status_code == 200
    assert len(list_resp.json()["items"]) == 1

    # 3. Teardown preview environment
    td_resp = client.post(
        f"/preview-environments/{prv_id}/teardown",
        headers={"X-Forwarded-User": "developer-alice"},
    )
    assert td_resp.status_code == 200
    assert td_resp.json()["status"] == "destroyed"


def test_self_service_resources_api(client: TestClient, monkeypatch):
    # Create application
    app_resp = client.post(
        "/applications",
        headers={"X-Forwarded-User": "developer-alice", "Idempotency-Key": f"app-key-{uuid4()}"},
        json={
            "name": "search-api",
            "repositoryUrl": "https://github.com/org/search-api",
            "pipelineTemplate": "container-ci-cd-v1",
            "runtime": "docker",
            "defaultEnvironment": "dev",
            "stages": [],
        },
    )
    app_id = app_resp.json()["id"]

    # 1. Unconfigured provider in preview env fails closed
    res_resp = client.post(
        "/self-service/resources",
        headers={"X-Forwarded-User": "developer-alice"},
        json={
            "applicationId": app_id,
            "teamId": "search-eng",
            "environment": "preview",
            "resourceType": "redis_cache",
            "spec": {"memory_mb": 512},
        },
    )
    assert res_resp.status_code == 201
    res_data = res_resp.json()
    req_id = res_data["requestId"]
    assert res_data["status"] == "provider_not_configured"
    assert "No infrastructure provider" in res_data["statusReason"]

    # 2. Staging / Production requires approval and enforces separation of duties
    monkeypatch.setenv("NETCI_RESOURCE_PROVIDER", "terraform")
    prod_resp = client.post(
        "/self-service/resources",
        headers={"X-Forwarded-User": "developer-alice"},
        json={
            "applicationId": app_id,
            "teamId": "search-eng",
            "environment": "production",
            "resourceType": "postgres_database",
            "spec": {"size_gb": 20},
            "requestedBy": "developer-alice",
        },
    )
    assert prod_resp.status_code == 201
    prod_data = prod_resp.json()
    prod_req_id = prod_data["requestId"]
    assert prod_data["status"] == "pending_approval"

    # Self-approval fails 403
    appr_fail = client.post(
        f"/self-service/resources/{prod_req_id}/approve",
        headers={"X-Forwarded-Roles": "reviewer"},
        json={"approvedBy": "developer-alice"},
    )
    assert appr_fail.status_code == 403
    assert appr_fail.json()["detail"]["code"] == "SEPARATION_OF_DUTIES"

    # Separate approval succeeds
    appr_ok = client.post(
        f"/self-service/resources/{prod_req_id}/approve",
        headers={"X-Forwarded-Roles": "reviewer"},
        json={"approvedBy": "lead-bob"},
    )
    assert appr_ok.status_code == 200
    assert appr_ok.json()["status"] == "ready"
    assert appr_ok.json()["approvedBy"] == "lead-bob"

    # Deprovision
    deprov = client.post(
        f"/self-service/resources/{prod_req_id}/deprovision",
        headers={"X-Forwarded-User": "developer-alice"},
    )
    assert deprov.status_code == 200
    assert deprov.json()["status"] == "deprovisioned"
