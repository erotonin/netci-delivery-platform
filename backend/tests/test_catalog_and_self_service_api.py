"""Integration tests for Phase 12: Catalog, Templates, Previews, and Self-Service APIs."""

import hashlib
import hmac
import json
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
    """A preview is a deployment now (ADR-049): it starts `deploying` from a real
    pull-request build and only the worker's own callback may say `active`. This test
    used to assert the opposite -- a preview born `active` with a URL nobody served,
    which is exactly the fake success this project forbids -- so it now drives the real
    trigger and callback instead of the removed body-supplied create."""

    secret = "webhook-secret-for-catalog-test"
    system_id = f"sys-{uuid4().hex[:6]}"
    assert client.post(
        "/systems", json={"id": system_id, "unit": "Cart", "description": "cart"}
    ).status_code == 201
    module_resp = client.post(
        f"/systems/{system_id}/modules",
        json={
            "name": "cart-api", "displayName": "cart-api",
            "repositoryUrl": "https://github.com/org/cart-api",
            "pipelineTemplate": "kubernetes-ci-cd-v1", "runtime": "kubernetes", "moduleType": "Backend",
            "pipelineConfig": {
                "runner": "jenkins", "strategy": "Trunk-based",
                "pipelines": {"ci": {"branch": "main", "stages": ["build"]}},
                "previews": {"enabled": True, "ttlHours": 2},
            },
            "deploymentEnvironments": [
                {"displayName": env, "environment": env, "runtime": "kubernetes",
                 "kubeconfigRef": f"{env}-kubeconfig", "namespace": f"ns-{env}"}
                for env in ("dev", "staging", "prod")
            ],
        },
    )
    assert module_resp.status_code == 201, module_resp.text
    module = module_resp.json()
    app_id = module["applicationId"]
    repo = "org/cart-api"
    scm_resp = client.post(
        f"/applications/{app_id}/scm",
        json={"provider": "github", "repositoryIdentity": repo, "secretToken": secret},
    )
    assert scm_resp.status_code == 201, scm_resp.text

    def github_hook(payload: dict):
        raw = json.dumps(payload).encode()
        signature = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
        return client.post(
            "/webhooks/scm/github", content=raw,
            headers={"x-github-delivery": uuid4().hex, "x-github-event": "pull_request",
                     "x-hub-signature-256": f"sha256={signature}", "content-type": "application/json"},
        )

    pr_payload = {
        "action": "opened", "repository": {"full_name": repo}, "sender": {"login": "dev1"},
        "pull_request": {"number": 55,
                         "head": {"sha": uuid4().hex + uuid4().hex[:8], "ref": "feature/x", "repo": {"full_name": repo}},
                         "base": {"ref": "main", "repo": {"full_name": repo}}},
    }
    triggered = github_hook(pr_payload)
    assert triggered.status_code == 201, triggered.text
    run_id = triggered.json()["pipelineRunId"]

    machine = {"Authorization": "Bearer netci-local-pipeline-key"}
    client.post(f"/pipeline-runs/{run_id}/ci-result", headers=machine, json={"status": "running"})
    digest = f"sha256:{uuid4().hex}{uuid4().hex}"
    evidence = client.post(f"/pipeline-runs/{run_id}/security-evidence", headers=machine, json={
        "artifactDigest": digest,
        "artifactRef": f"registry.local/cart-api@{digest}",
        "sbom": {"generatedBy": "syft", "location": "s3://evidence/sbom.json", "format": "cyclonedx-json"},
        "vulnerabilityScan": {"scanner": "trivy", "status": "passed", "critical": 0, "high": 0, "medium": 0},
        "signature": {"provider": "cosign", "verified": True, "certificateIdentity": "netci"},
    })
    assert evidence.status_code == 202, evidence.text
    succeeded = client.post(f"/pipeline-runs/{run_id}/ci-result", headers=machine, json={
        "status": "succeeded", "artifactDigest": digest,
    })
    assert succeeded.status_code == 202, succeeded.text

    # 1. The trigger started a preview -- `deploying`, no invented URL.
    list_resp = client.get(f"/preview-environments?applicationId={app_id}")
    assert list_resp.status_code == 200
    items = list_resp.json()["items"]
    assert len(items) == 1
    prv_data = items[0]
    prv_id = prv_data["previewId"]
    assert prv_data["status"] == "deploying"
    assert prv_data["url"] is None
    assert "pr-55" in prv_id
    assert prv_data["pipelineRunId"] == run_id

    # 2. Get by id agrees.
    get_resp = client.get(f"/preview-environments/{prv_id}")
    assert get_resp.status_code == 200
    assert get_resp.json()["previewId"] == prv_id

    # 3. Teardown moves it to `destroying`; only the worker's own report ends it.
    td_resp = client.post(
        f"/preview-environments/{prv_id}/teardown",
        headers={"X-Forwarded-User": "developer-alice"},
    )
    assert td_resp.status_code == 200
    assert td_resp.json()["status"] == "destroying"

    # No NETCI_WORKLOAD_TOKEN_KEYS configured in this test, same as the ci-result calls
    # above: the legacy shared key authenticates as the pipeline role and carries no
    # per-run claims to check (workload-token scoping is covered in test_real_previews.py).
    result_resp = client.post(
        f"/preview-environments/{prv_id}/result", headers=machine,
        json={"status": "destroyed", "message": "namespace removed"},
    )
    assert result_resp.status_code == 202, result_resp.text
    assert result_resp.json()["status"] == "destroyed"


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


def _template(**overrides):
    body = {"templateId": "orders-service", "version": "v1.0.0", "name": "Orders Service",
            "description": "golden path", "category": "backend",
            "parametersSchema": {"type": "object", "properties": {"port": {"type": "integer"}}},
            "pipelineDefinition": {"stages": ["checkout", "build"]}, "isDeprecated": False}
    body.update(overrides)
    return body


def test_a_registered_template_version_cannot_be_rewritten(client: TestClient):
    """Registering an existing version used to overwrite it, definition and all.

    Every module instantiated from orders-service v1.0.0 would then name a pipeline it was
    never built from. A version is written once; a changed definition is a new version.
    """

    assert client.post("/catalog/templates", json=_template()).status_code == 201

    rewritten = client.post("/catalog/templates", json=_template(pipelineDefinition={"stages": ["deploy-anything"]}))
    assert rewritten.status_code == 409, rewritten.text
    assert rewritten.json()["code"] == "TEMPLATE_VERSION_EXISTS"

    stored = client.get("/catalog/templates/orders-service", params={"version": "v1.0.0"}).json()
    assert stored["pipelineDefinition"] == {"stages": ["checkout", "build"]}


def test_a_template_version_can_still_be_retired(client: TestClient):
    """Deprecation is the one change an existing version admits, and only with its content intact."""

    assert client.post("/catalog/templates", json=_template()).status_code == 201
    retired = client.post("/catalog/templates", json=_template(isDeprecated=True))
    assert retired.status_code == 201, retired.text
    assert retired.json()["isDeprecated"] is True


def test_registering_the_same_version_again_is_idempotent(client: TestClient):
    assert client.post("/catalog/templates", json=_template()).status_code == 201
    again = client.post("/catalog/templates", json=_template())
    assert again.status_code == 201
    assert again.json()["pipelineDefinition"] == {"stages": ["checkout", "build"]}
