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


def _setup_verified_module(client, auth_headers, system_id, module_name, version_tag, runtime="kubernetes"):
    if runtime == "kubernetes":
        environments = [
            {"displayName": "Dev", "environment": "dev", "runtime": "kubernetes", "servers": [],
             "kubeconfigRef": "netci-kubeconfig", "namespace": "dev"},
            {"displayName": "Prod", "environment": "prod", "runtime": "kubernetes", "servers": [],
             "kubeconfigRef": "netci-kubeconfig", "namespace": "prod"},
        ]
        template = "kubernetes-ci-cd-v1"
    else:
        environments = [
            {"displayName": "Dev", "environment": "dev", "runtime": runtime, "servers": ["dev-host"]},
            {"displayName": "Prod", "environment": "prod", "runtime": runtime, "servers": ["prod-host"]},
        ]
        template = "container-ci-cd-v1"
    module_res = client.post(
        f"/systems/{system_id}/modules",
        headers=auth_headers,
        json={
            "name": module_name,
            "displayName": module_name,
            "repositoryUrl": f"https://git.example.com/team/{module_name}",
            "pipelineTemplate": template,
            "runtime": runtime,
            "moduleType": "Backend",
            "description": f"Test module {module_name}",
            "deploymentEnvironments": environments,
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

    # The intended weight is recorded with the deployment; the router has applied
    # nothing yet, because the canary release does not exist until the worker reports.
    traffic_res = client.get(f"/deployments/{dep_id}/traffic", headers=auth_headers)
    assert traffic_res.status_code == 200, traffic_res.text
    traffic_data = traffic_res.json()
    assert traffic_data["strategy"] == "canary"
    assert traffic_data["trafficWeight"] == 10
    assert traffic_data["routerStatus"]["canaryWeight"] == 0

    # The deployment carries the canary track and its first weight for the playbook.
    from backend.app.main import platform, portal
    canary_dep = platform.get_deployment(UUID(dep_id))
    canary_run = platform.get_pipeline(canary_dep.pipeline_run_id)
    assert canary_run.parameters["release_track"] == "canary"
    assert canary_run.parameters["canary_weight"] == 10

    # Worker reports the canary release healthy -> the router confirms 10 %.
    platform.record_deployment_result(UUID(dep_id), "healthy", "canary up", fencing_token=canary_dep.fencing_token)
    portal.record_production_deployment_result(UUID(dep_id), "healthy", "canary up")
    traffic_data = client.get(f"/deployments/{dep_id}/traffic", headers=auth_headers).json()
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

    # Verify traffic immediately rolled back to 0% in router, and the canary release
    # retired through the ordinary rollback path (it was healthy, so it was running).
    assert fail_data["canaryReleaseRetired"] is True
    traffic_res3 = client.get(f"/deployments/{dep_id}/traffic", headers=auth_headers)
    assert traffic_res3.json()["trafficWeight"] == 0
    assert traffic_res3.json()["routerStatus"]["canaryWeight"] == 0
    assert platform.get_deployment(UUID(dep_id)).status.value == "rollback_in_progress"


def test_canary_last_step_promotes_the_stable_release(client, auth_headers, dev_headers, reviewer_headers):
    sys_res = client.post(
        "/systems",
        headers=auth_headers,
        json={"id": f"sys-{uuid4().hex[:8]}", "unit": "Search", "description": "Canary promotion"},
    )
    system_id = sys_res.json()["id"]
    mod_id, app_id, _ = _setup_verified_module(client, auth_headers, system_id, "search-promote", "v2.1.0")
    sched = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    req_id = client.post(
        "/production-requests",
        headers=dev_headers,
        json={
            "modules": [{"moduleId": mod_id, "version": "v2.1.0"}],
            "scheduledFor": sched,
            "rollbackStrategy": "automatic",
            "runAutomationTests": True,
            "strategy": "canary",
            "strategyConfig": {"steps": [50, 100]},
        },
    ).json()["id"]
    dep_id = client.post(
        f"/production-requests/{req_id}/approve", headers=reviewer_headers, json={"comment": "go"}
    ).json()["modules"][0]["deploymentId"]

    from backend.app.main import platform, portal
    canary_dep = platform.get_deployment(UUID(dep_id))
    platform.record_deployment_result(UUID(dep_id), "healthy", "canary up", fencing_token=canary_dep.fencing_token)
    portal.record_production_deployment_result(UUID(dep_id), "healthy", "canary up")

    adv = client.post(
        f"/production-requests/{req_id}/canary/advance",
        headers=reviewer_headers,
        json={"metrics": {"errorRate": 0.0, "p95LatencyMs": 50.0}},
    )
    assert adv.status_code == 200, adv.text
    body = adv.json()
    assert body["status"] == "promoting"
    assert body["trafficWeight"] == 100
    promotion = platform.get_deployment(UUID(body["promotionDeploymentId"]))
    assert promotion.artifact_digest == canary_dep.artifact_digest
    assert promotion.status.value == "deploying"
    promotion_run = platform.get_pipeline(promotion.pipeline_run_id)
    assert promotion_run.parameters["release_track"] == "promote"
    # The reviewer who advanced the canary is the approver of the promotion.
    plan = client.get(f"/production-requests/{req_id}/plan", headers=auth_headers).json()
    assert plan["deploymentId"] == str(promotion.id)
    assert "promoting" in plan["comment"]


def test_blue_green_delivery_flow(client, auth_headers, dev_headers, reviewer_headers):
    # Setup System
    sys_res = client.post(
        "/systems",
        headers=auth_headers,
        json={"id": f"sys-{uuid4().hex[:8]}", "unit": "Checkout", "description": "Blue Green test"},
    )
    assert sys_res.status_code == 201, sys_res.text
    system_id = sys_res.json()["id"]

    mod_id, app_id, _ = _setup_verified_module(client, auth_headers, system_id, "checkout-ui", "v3.0.0", runtime="docker")

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

    # Approve -> dispatches the release into the colour that is not serving (blue is,
    # so green), with the track as a server-decided parameter; nothing is switched yet.
    appr_res = client.post(f"/production-requests/{req_id}/approve", headers=reviewer_headers, json={"comment": "approve blue green"})
    assert appr_res.status_code == 202, appr_res.text
    dep_id = appr_res.json()["modules"][0]["deploymentId"]

    traffic_res = client.get(f"/deployments/{dep_id}/traffic", headers=auth_headers)
    assert traffic_res.status_code == 200, traffic_res.text
    assert traffic_res.json()["activeColor"] == "green"
    assert traffic_res.json()["routerStatus"]["activeColor"] == "blue"
    from backend.app.main import platform, portal
    dep = platform.get_deployment(UUID(dep_id))
    assert platform.get_pipeline(dep.pipeline_run_id).parameters["release_track"] == "green"

    # Healthy -> the stable ingress is switched to green.
    platform.record_deployment_result(UUID(dep_id), "healthy", "green up", fencing_token=dep.fencing_token)
    portal.record_production_deployment_result(UUID(dep_id), "healthy", "green up")
    assert client.get(f"/deployments/{dep_id}/traffic", headers=auth_headers).json()["routerStatus"]["activeColor"] == "green"

    # Switch-back is one call, recorded on the deployment; only blue/green deployments have colours.
    back = client.post(f"/deployments/{dep_id}/traffic/switch", headers=reviewer_headers, json={"activeColor": "blue"})
    assert back.status_code == 200, back.text
    assert back.json()["previousColor"] == "green" and back.json()["routerStatus"]["activeColor"] == "blue"
    assert client.get(f"/deployments/{dep_id}/traffic", headers=auth_headers).json()["activeColor"] == "blue"


def test_canary_is_refused_for_a_runtime_without_a_traffic_router(client, auth_headers, dev_headers, reviewer_headers):
    """A docker host has nothing in front of it that splits traffic: a canary there would be
    a full rollout reporting a weight."""

    sys_res = client.post(
        "/systems",
        headers=auth_headers,
        json={"id": f"sys-{uuid4().hex[:8]}", "unit": "Search", "description": "Canary on docker"},
    )
    system_id = sys_res.json()["id"]
    mod_id, _, _ = _setup_verified_module(client, auth_headers, system_id, "search-docker", "v2.2.0", runtime="docker")
    sched = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    req_id = client.post(
        "/production-requests",
        headers=dev_headers,
        json={
            "modules": [{"moduleId": mod_id, "version": "v2.2.0"}],
            "scheduledFor": sched,
            "rollbackStrategy": "automatic",
            "runAutomationTests": True,
            "strategy": "canary",
        },
    ).json()["id"]
    approve = client.post(f"/production-requests/{req_id}/approve", headers=reviewer_headers, json={"comment": "go"})
    assert approve.status_code != 202, approve.text
    assert "kubernetes runtime" in approve.text
    from backend.app.main import portal
    assert portal.production_request(req_id)["status"] != "succeeded"


def test_colour_switch_is_refused_for_a_deployment_without_colours(client, auth_headers, dev_headers, reviewer_headers):
    sys_res = client.post("/systems", headers=auth_headers, json={"id": f"sys-{uuid4().hex[:8]}", "unit": "Search", "description": "no colours"})
    mod_id, _, _ = _setup_verified_module(client, auth_headers, sys_res.json()["id"], "search-rolling", "v2.3.0")
    req_id = client.post("/production-requests", headers=dev_headers, json={
        "modules": [{"moduleId": mod_id, "version": "v2.3.0"}], "scheduledFor": datetime.now(timezone.utc).isoformat(),
        "rollbackStrategy": "automatic", "runAutomationTests": True, "strategy": "rolling",
    }).json()["id"]
    dep_id = client.post(f"/production-requests/{req_id}/approve", headers=reviewer_headers, json={"comment": "go"}).json()["modules"][0]["deploymentId"]
    refused = client.post(f"/deployments/{dep_id}/traffic/switch", headers=reviewer_headers, json={"activeColor": "green"})
    assert refused.status_code == 409 and refused.json()["code"] == "NOT_BLUE_GREEN"
