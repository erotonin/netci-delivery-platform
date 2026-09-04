# backend/tests/test_progressive_delivery.py
import pytest
from datetime import datetime, timezone, timedelta
from uuid import uuid4, UUID
from fastapi.testclient import TestClient

from backend.app.main import app
from backend.app.domain.models import Environment, PipelineStatus
from backend.app.store.postgres import PostgresDatabase
from backend.app.traffic import default_traffic_router

TEST_DATABASE_URL = "postgresql://netci:netci-local-only@127.0.0.1:55432/netci"


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def auth_headers():
    return {"Authorization": "Bearer platform-admin-token"}


@pytest.fixture
def dev_headers():
    return {"Authorization": "Bearer developer-token"}


@pytest.fixture
def reviewer_headers():
    return {"Authorization": "Bearer release-manager-token"}


def _setup_verified_module(client, auth_headers, system_id, module_name, version_tag):
    module_res = client.post(
        f"/systems/{system_id}/modules",
        headers=auth_headers,
        json={
            "name": module_name,
            "displayName": module_name,
            "repositoryUrl": f"https://git.example.com/team/{module_name}",
            "pipelineTemplate": "container-ci-cd-v1",
            "runtime": "docker",
            "moduleType": "Backend",
            "description": f"Test module {module_name}",
            "deploymentEnvironments": [
                {"displayName": "Dev", "environment": "dev", "runtime": "docker", "servers": ["dev-host"]},
                {"displayName": "Prod", "environment": "prod", "runtime": "docker", "servers": ["prod-host"]},
            ],
        },
    )
    assert module_res.status_code == 201, module_res.text
    module_data = module_res.json()
    module_id = module_data["id"]
    app_id = module_data["applicationId"]

    run_id = str(uuid4())
    artifact_digest = f"sha256:{uuid4().hex}{uuid4().hex}"
    from backend.app.main import platform
    with platform.transaction() as tx:
        from backend.app.domain.models import PipelineRun
        now = datetime.now(timezone.utc)
        run = PipelineRun(
            id=UUID(run_id),
            application_id=UUID(app_id),
            commit_sha="b" * 40,
            branch="main",
            environment=Environment.PROD,
            parameters={},
            correlation_id=f"test:{run_id}",
            status=PipelineStatus.SUCCEEDED,
            started_by="ci-machine",
            artifact_digest=artifact_digest,
            created_at=now,
            updated_at=now,
        )
        from backend.app.persistence import UnitOfWork
        uow = UnitOfWork()
        uow.runs.append((run, None))
        uow.security_evidence.append((
            UUID(run_id),
            UUID(app_id),
            artifact_digest,
            {
                "artifactDigest": artifact_digest,
                "decision": "allow",
                "signature": {"provider": "cosign", "verified": True},
                "sbom": {"format": "cyclonedx-json", "location": "s3://netci/sbom.json", "generatedBy": "syft"},
                "vulnerabilityScan": {"scanner": "trivy", "status": "passed", "critical": 0, "high": 0},
            },
        ))
        tx.apply(uow)

    ver_res = client.post(
        f"/modules/{module_id}/versions",
        headers=auth_headers,
        json={
            "tag": version_tag,
            "gitTagUrl": f"https://github.com/org/repo/releases/tag/{version_tag}",
            "artifactUrl": f"https://registry.internal/repo:{version_tag}",
            "pipelineRunId": run_id,
            "artifactDigest": artifact_digest,
        },
    )
    assert ver_res.status_code == 201, ver_res.text

    rep_res = client.post(
        f"/modules/{module_id}/versions/{version_tag}/ci-report",
        headers={"Authorization": "Bearer netci-local-pipeline-key"},
        json={
            "coverage": 90.0,
            "autoTest": "passed",
            "sast": "passed",
            "sastIssues": 0,
            "vulnerabilities": {"critical": 0, "high": 0, "medium": 0},
            "commit": "a" * 40,
        },
    )
    assert rep_res.status_code == 202, rep_res.text

    return module_id, app_id, run_id


def test_canary_progressive_delivery_flow(client, auth_headers, dev_headers, reviewer_headers):
    # Setup System
    sys_res = client.post(
        "/systems",
        headers=auth_headers,
        json={"id": f"sys-{uuid4().hex[:8]}", "unit": "Search", "description": "Canary progressive delivery"},
    )
    assert sys_res.status_code == 201, sys_res.text
    system_id = sys_res.json()["id"]

    mod_id, app_id, _ = _setup_verified_module(client, auth_headers, system_id, "search-api", "v2.0.0")

    sched = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    req_res = client.post(
        "/production-requests",
        headers=dev_headers,
        json={
            "modules": [{"moduleId": mod_id, "version": "v2.0.0"}],
            "scheduledFor": sched,
            "rollbackStrategy": "automatic",
            "runAutomationTests": True,
            "strategy": "canary",
            "strategyConfig": {
                "steps": [10, 25, 50, 100],
                "thresholds": {"maxErrorRate": 0.05, "maxP95LatencyMs": 500},
            },
        },
    )
    assert req_res.status_code == 201, req_res.text
    req_id = req_res.json()["id"]

    # Approve request -> starts canary at initial step (10%)
    appr_res = client.post(f"/production-requests/{req_id}/approve", headers=reviewer_headers, json={"comment": "approve canary"})
    assert appr_res.status_code == 202, appr_res.text
    dep_id = appr_res.json()["modules"][0]["deploymentId"]

    # Verify initial traffic
    traffic_res = client.get(f"/deployments/{dep_id}/traffic", headers=auth_headers)
    assert traffic_res.status_code == 200, traffic_res.text
    traffic_data = traffic_res.json()
    assert traffic_data["strategy"] == "canary"
    assert traffic_data["trafficWeight"] == 10
    assert traffic_data["routerStatus"]["canaryWeight"] == 10
    assert traffic_data["routerStatus"]["baselineWeight"] == 90

    # Advance canary to step 2 (25%) with healthy metrics
    adv_res = client.post(
        f"/production-requests/{req_id}/canary/advance",
        headers=reviewer_headers,
        json={"metrics": {"errorRate": 0.005, "p95LatencyMs": 120.0}},
    )
    assert adv_res.status_code == 200, adv_res.text
    adv_data = adv_res.json()
    assert adv_data["status"] == "advanced"
    assert adv_data["trafficWeight"] == 25

    # Verify updated traffic in router
    traffic_res2 = client.get(f"/deployments/{dep_id}/traffic", headers=auth_headers)
    assert traffic_res2.json()["trafficWeight"] == 25
    assert traffic_res2.json()["routerStatus"]["canaryWeight"] == 25

    # Advance canary with failing metrics (error rate 8% > 5% threshold)
    fail_adv = client.post(
        f"/production-requests/{req_id}/canary/advance",
        headers=reviewer_headers,
        json={"metrics": {"errorRate": 0.08, "p95LatencyMs": 150.0}},
    )
    assert fail_adv.status_code == 200, fail_adv.text
    fail_data = fail_adv.json()
    assert fail_data["status"] == "aborted"
    assert not fail_data["allowed"]

    # Verify traffic immediately rolled back to 0% in router
    traffic_res3 = client.get(f"/deployments/{dep_id}/traffic", headers=auth_headers)
    assert traffic_res3.json()["trafficWeight"] == 0
    assert traffic_res3.json()["routerStatus"]["canaryWeight"] == 0


def test_blue_green_delivery_flow(client, auth_headers, dev_headers, reviewer_headers):
    # Setup System
    sys_res = client.post(
        "/systems",
        headers=auth_headers,
        json={"id": f"sys-{uuid4().hex[:8]}", "unit": "Checkout", "description": "Blue Green test"},
    )
    assert sys_res.status_code == 201, sys_res.text
    system_id = sys_res.json()["id"]

    mod_id, app_id, _ = _setup_verified_module(client, auth_headers, system_id, "checkout-ui", "v3.0.0")

    sched = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    req_res = client.post(
        "/production-requests",
        headers=dev_headers,
        json={
            "modules": [{"moduleId": mod_id, "version": "v3.0.0"}],
            "scheduledFor": sched,
            "rollbackStrategy": "automatic",
            "runAutomationTests": True,
            "strategy": "blue_green",
        },
    )
    assert req_res.status_code == 201, req_res.text
    req_id = req_res.json()["id"]

    # Approve -> dispatches green deployment
    appr_res = client.post(f"/production-requests/{req_id}/approve", headers=reviewer_headers, json={"comment": "approve blue green"})
    assert appr_res.status_code == 202, appr_res.text
    dep_id = appr_res.json()["modules"][0]["deploymentId"]

    # Verify active color is green
    traffic_res = client.get(f"/deployments/{dep_id}/traffic", headers=auth_headers)
    assert traffic_res.status_code == 200, traffic_res.text
    assert traffic_res.json()["activeColor"] == "green"
    assert traffic_res.json()["routerStatus"]["activeColor"] == "green"
