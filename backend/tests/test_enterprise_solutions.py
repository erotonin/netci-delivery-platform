# backend/tests/test_enterprise_solutions.py
import asyncio
import json
import pytest
from datetime import datetime, timezone, timedelta
from uuid import uuid4, UUID
from fastapi.testclient import TestClient

from backend.app.main import app
from backend.app.domain.models import Environment, PipelineStatus, PipelineRun
from backend.app.persistence import UnitOfWork


@pytest.fixture
def client(monkeypatch):
    # Agent tokens are signed; the suite needs a key to mint and verify them.
    monkeypatch.setenv("NETCI_WORKLOAD_TOKEN_KEYS", "k1:" + "s" * 48)
    return TestClient(app)


@pytest.fixture
def auth_headers():
    return {"Authorization": "Bearer admin-token"}


@pytest.fixture
def dev_headers():
    return {"Authorization": "Bearer dev-token"}


@pytest.fixture
def reviewer_headers():
    return {"Authorization": "Bearer admin-token"}


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
    return module_id, app_id, artifact_digest, run_id


def test_config_only_fast_apply(client, auth_headers, dev_headers, reviewer_headers):
    # Setup test system
    sys_id = f"sys-{uuid4().hex[:8]}"
    sys_res = client.post(
        "/systems",
        headers=auth_headers,
        json={"id": sys_id, "unit": "CorePlatform", "description": "Fast apply test"},
    )
    assert sys_res.status_code == 201, sys_res.text

    mod_id, app_id, digest, _ = _setup_verified_module(client, auth_headers, sys_id, f"mod-cfg-{uuid4().hex[:6]}", "v1.0.0")

    # Propose revision
    rev_res = client.post(
        f"/modules/{mod_id}/config-revisions",
        headers=dev_headers,
        json={
            "changeSummary": "Update memory limit to 2Gi",
            "pipelineConfig": {"memoryLimit": "2Gi", "replicas": 4},
            "deploymentConfig": [{"environment": "prod", "servers": ["prod-host"], "memory": "2Gi"}],
        },
    )
    assert rev_res.status_code == 201, rev_res.text
    rev_data = rev_res.json()
    rev_id = rev_data["id"]
    rev_num = rev_data["revisionNumber"]

    # If pending approval, approve it
    if rev_data.get("requiresApproval"):
        appr_res = client.post(
            f"/modules/{mod_id}/config-revisions/{rev_id}/approve",
            headers=reviewer_headers,
        )
        assert appr_res.status_code == 200, appr_res.text

    # Fast apply config to prod (without CI rebuild)
    apply_res = client.post(
        f"/modules/{mod_id}/config/apply",
        headers=dev_headers,
        json={"environment": "prod", "revisionId": rev_id},
    )
    assert apply_res.status_code == 200, apply_res.text
    result = apply_res.json()

    # A production redeploy waits for approval like any other; nothing is "healthy" until
    # a worker has deployed it and said so. The old response asserted `healthy` and a
    # two-second lead time for a deployment that had touched no runtime.
    assert result["status"] == "pending_approval"
    assert result["configBypassedCi"] is True
    assert result["artifactDigest"] == digest  # the real artifact, reused -- never invented
    assert result["revisionNumber"] == rev_num
    assert "leadTimeSeconds" not in result
    deployment = client.get(f"/deployments/{result['deploymentId']}", headers=dev_headers).json()
    assert deployment["status"] == "pending_approval"
    assert deployment["artifactDigest"] == digest


def test_config_apply_refuses_when_nothing_was_ever_built(client, auth_headers, dev_headers):
    """The old code hashed the module name into a digest and deployed that."""

    sys_id = f"sys-{uuid4().hex[:8]}"
    client.post("/systems", headers=auth_headers, json={"id": sys_id, "unit": "CorePlatform", "description": "no build"})
    mod = client.post(
        f"/systems/{sys_id}/modules", headers=auth_headers,
        json={
            "name": f"mod-nobuild-{uuid4().hex[:6]}", "repositoryUrl": "https://git.example.com/t/x",
            "pipelineTemplate": "container-ci-cd-v1", "runtime": "docker", "moduleType": "Backend",
            "deploymentEnvironments": [{"displayName": "Dev", "environment": "dev", "runtime": "docker", "servers": ["dev-host"]}],
        },
    ).json()

    refused = client.post(f"/modules/{mod['id']}/config/apply", headers=dev_headers, json={"environment": "dev"})

    assert refused.status_code == 409
    assert refused.json()["code"] == "NO_DEPLOYABLE_ARTIFACT"
    assert client.get(f"/applications/{mod['applicationId']}/dora", headers=dev_headers).json()["sourceEvents"] == 0


def test_agent_websocket_telemetry_and_script(client, auth_headers):
    # Test installer script endpoint
    install_res = client.get("/api/v1/agents/install.sh")
    assert install_res.status_code == 200
    assert "netCI Edge Runner Agent Installer" in install_res.text

    # Test WebSocket connection and telemetry -- with the token an admin minted for this host.
    host_key = f"edge-worker-{uuid4().hex[:6]}"
    token = client.post("/api/v1/agents/token", headers=auth_headers, json={"hostname": host_key}).json()["token"]
    with client.websocket_connect(f"/api/v1/agents/ws?agent_id=ag-1&token={token}") as ws:
        # Send telemetry heartbeat
        telem_msg = {
            "type": "TELEMETRY_HEARTBEAT",
            "agent_id": "ag-1",
            "hostname": host_key,
            "telemetry": {
                "cpu_percent": 18.5,
                "mem_percent": 42.0,
                "disk_percent": 65.2,
            },
        }
        ws.send_text(json.dumps(telem_msg))
        ack = json.loads(ws.receive_text())
        assert ack["type"] == "HEARTBEAT_ACK"

        # Check status endpoint
        status_res = client.get("/api/v1/agents/status", headers=auth_headers)
        assert status_res.status_code == 200
        items = status_res.json()["items"]
        agent = next((a for a in items if a["hostname"] == host_key), None)
        assert agent is not None
        assert agent["telemetry"]["cpuPercent"] == 18.5
        assert agent["telemetry"]["memPercent"] == 42.0


def test_multi_module_dag_plan_waves(client, auth_headers, dev_headers):
    sys_id = f"sys-{uuid4().hex[:8]}"
    sys_res = client.post(
        "/systems",
        headers=auth_headers,
        json={"id": sys_id, "unit": "CorePlatform", "description": "DAG wave test"},
    )
    assert sys_res.status_code == 201, sys_res.text

    mod_db, _, _, _ = _setup_verified_module(client, auth_headers, sys_id, f"mod-db-{uuid4().hex[:6]}", "v1.0.0")
    mod_api, _, _, _ = _setup_verified_module(client, auth_headers, sys_id, f"mod-api-{uuid4().hex[:6]}", "v1.0.0")

    # Create multi-module request with API depending on DB
    sched = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    req_res = client.post(
        "/production-requests",
        headers=dev_headers,
        json={
            "modules": [
                {"moduleId": mod_api, "version": "v1.0.0", "dependencies": [mod_db], "deploymentOrder": 2},
                {"moduleId": mod_db, "version": "v1.0.0", "dependencies": [], "deploymentOrder": 1},
            ],
            "scheduledFor": sched,
            "rollbackStrategy": "automatic",
            "runAutomationTests": True,
            "strategy": "rolling",
        },
    )
    assert req_res.status_code == 201, req_res.text
    req_data = req_res.json()

    assert len(req_data["modules"]) == 2
    plan = req_data["releasePlan"]
    assert plan["totalWaves"] == 2
    assert plan["waves"][0]["moduleIds"] == [mod_db]
    assert plan["waves"][1]["moduleIds"] == [mod_api]
