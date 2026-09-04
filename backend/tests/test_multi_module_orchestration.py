# backend/tests/test_multi_module_orchestration.py
import pytest
from datetime import datetime, timezone, timedelta
from uuid import uuid4, UUID
from fastapi.testclient import TestClient

from backend.app.main import app
from backend.app.domain.models import Environment, PipelineStatus, Runtime
from backend.app.portal import PortalService
from backend.app.store.postgres import PostgresDatabase

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
    # 1. Create module
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

    # 2. Register verified pipeline run
    run_id = str(uuid4())
    artifact_digest = f"sha256:{uuid4().hex}{uuid4().hex}"
    from backend.app.main import platform
    with platform.transaction() as tx:
        # Create pipeline run directly in db
        from backend.app.domain.models import PipelineRun
        now = datetime.now(timezone.utc)
        run = PipelineRun(
            id=UUID(run_id),
            application_id=UUID(app_id),
            commit_sha="a" * 40,
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

    # 3. Register version with CI report
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

    # 4. Attach passing automation report
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


def test_multi_module_dag_creation_and_wave_execution(client, auth_headers, dev_headers, reviewer_headers):
    # Setup System
    sys_res = client.post(
        "/systems",
        headers=auth_headers,
        json={"id": f"sys-{uuid4().hex[:8]}", "unit": "Payments", "description": "Payment multi-module test"},
    )
    assert sys_res.status_code == 201, sys_res.text
    system_id = sys_res.json()["id"]

    # Setup 3 modules: db_mod (wave 1), api_mod (wave 2, depends on db_mod), web_mod (wave 3, depends on api_mod)
    db_id, _, _ = _setup_verified_module(client, auth_headers, system_id, "db-service", "v1.0.0")
    api_id, _, _ = _setup_verified_module(client, auth_headers, system_id, "api-service", "v1.0.0")
    web_id, _, _ = _setup_verified_module(client, auth_headers, system_id, "web-service", "v1.0.0")

    # Create multi-module production request with DAG dependencies
    sched = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
    req_res = client.post(
        "/production-requests",
        headers=dev_headers,
        json={
            "modules": [
                {"moduleId": web_id, "version": "v1.0.0", "dependencies": [api_id]},
                {"moduleId": api_id, "version": "v1.0.0", "dependencies": [db_id]},
                {"moduleId": db_id, "version": "v1.0.0", "dependencies": []},
            ],
            "scheduledFor": sched,
            "rollbackStrategy": "automatic",
            "runAutomationTests": True,
            "strategy": "rolling",
        },
    )
    assert req_res.status_code == 201, req_res.text
    req_data = req_res.json()
    req_id = req_data["id"]
    assert len(req_data["modules"]) == 3

    # Check release plan endpoint
    plan_res = client.get(f"/production-requests/{req_id}/plan", headers=auth_headers)
    assert plan_res.status_code == 200, plan_res.text
    plan_data = plan_res.json()
    assert plan_data["releasePlan"]["totalWaves"] == 3
    assert plan_data["releasePlan"]["waves"][0]["moduleIds"] == [db_id]
    assert plan_data["releasePlan"]["waves"][1]["moduleIds"] == [api_id]
    assert plan_data["releasePlan"]["waves"][2]["moduleIds"] == [web_id]

    # Approve production request -> dispatches Wave 1 (db_id)
    appr_res = client.post(
        f"/production-requests/{req_id}/approve",
        headers=reviewer_headers,
        json={"comment": "Approved multi-module promotion"},
    )
    assert appr_res.status_code == 202, appr_res.text
    appr_data = appr_res.json()
    assert appr_data["status"] == "approved"

    # Verify db_mod is deploying
    db_mod = next(m for m in appr_data["modules"] if m["moduleId"] == db_id)
    assert db_mod["status"] == "deploying"
    assert db_mod["deploymentId"] is not None
    db_dep_id = db_mod["deploymentId"]

    # Other modules still pending
    api_mod = next(m for m in appr_data["modules"] if m["moduleId"] == api_id)
    assert api_mod["status"] == "pending"

    # Simulate db_mod deployment completing successfully
    cb_headers = {"Authorization": "Bearer netci-local-pipeline-key"}
    db_done = client.post(
        f"/deployments/{db_dep_id}/result",
        headers=cb_headers,
        json={"status": "healthy", "message": "Database migration succeeded"},
    )
    assert db_done.status_code == 202, db_done.text

    # Now verify Wave 2 (api_mod) was automatically dispatched!
    plan_after = client.get(f"/production-requests/{req_id}/plan", headers=auth_headers).json()
    db_mod_after = next(m for m in plan_after["modules"] if m["moduleId"] == db_id)
    api_mod_after = next(m for m in plan_after["modules"] if m["moduleId"] == api_id)
    assert db_mod_after["status"] == "succeeded"
    assert api_mod_after["status"] == "deploying"
    assert api_mod_after["deploymentId"] is not None


def test_multi_module_saga_compensation_on_failure(client, auth_headers, dev_headers, reviewer_headers):
    # Setup System
    sys_res = client.post(
        "/systems",
        headers=auth_headers,
        json={"id": f"sys-{uuid4().hex[:8]}", "unit": "Core", "description": "SAGA test"},
    )
    assert sys_res.status_code == 201, sys_res.text
    system_id = sys_res.json()["id"]

    mod1_id, _, _ = _setup_verified_module(client, auth_headers, system_id, "core-mod1", "v1.0.0")
    mod2_id, _, _ = _setup_verified_module(client, auth_headers, system_id, "core-mod2", "v1.0.0")

    sched = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
    req_res = client.post(
        "/production-requests",
        headers=dev_headers,
        json={
            "modules": [
                {"moduleId": mod1_id, "version": "v1.0.0", "dependencies": []},
                {"moduleId": mod2_id, "version": "v1.0.0", "dependencies": [mod1_id]},
            ],
            "scheduledFor": sched,
            "rollbackStrategy": "automatic",
            "runAutomationTests": True,
            "strategy": "rolling",
        },
    )
    assert req_res.status_code == 201, req_res.text
    req_id = req_res.json()["id"]

    # Approve -> starts wave 1 (mod1)
    client.post(f"/production-requests/{req_id}/approve", headers=reviewer_headers, json={"comment": "approve"})
    plan1 = client.get(f"/production-requests/{req_id}/plan", headers=auth_headers).json()
    mod1 = next(m for m in plan1["modules"] if m["moduleId"] == mod1_id)
    mod1_dep_id = mod1["deploymentId"]

    # Complete wave 1 successfully
    cb_headers = {"Authorization": "Bearer netci-local-pipeline-key"}
    client.post(f"/deployments/{mod1_dep_id}/result", headers=cb_headers, json={"status": "healthy"})

    # Wave 2 (mod2) is now deploying
    plan2 = client.get(f"/production-requests/{req_id}/plan", headers=auth_headers).json()
    mod2 = next(m for m in plan2["modules"] if m["moduleId"] == mod2_id)
    mod2_dep_id = mod2["deploymentId"]
    assert mod2["status"] == "deploying"

    # Now simulate mod2 failing health check!
    fail_res = client.post(
        f"/deployments/{mod2_dep_id}/result",
        headers=cb_headers,
        json={"status": "failed", "message": "Out of memory error during bootstrap"},
    )
    assert fail_res.status_code == 202, fail_res.text

    # Verify SAGA reverse rollback: mod1 has been compensated and request is marked blocked/failed!
    final_req = client.get(f"/production-requests/{req_id}/plan", headers=auth_headers).json()
    assert final_req["status"] in ("blocked", "rejected")
    final_mod1 = next(m for m in final_req["modules"] if m["moduleId"] == mod1_id)
    final_mod2 = next(m for m in final_req["modules"] if m["moduleId"] == mod2_id)
    assert final_mod2["status"] == "failed"
    assert final_mod1["status"] == "rolled_back"
