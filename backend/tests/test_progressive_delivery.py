# backend/tests/test_progressive_delivery.py
import pytest
from datetime import datetime, timezone, timedelta
from uuid import uuid4, UUID
from fastapi.testclient import TestClient

from backend.app.main import app
from backend.app.adapters.prometheus_metrics import MetricsUnavailable
from backend.app.domain.models import Environment, PipelineStatus
from backend.app.domain.verification import VerificationResult, parse_verification
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


class _FakeMetricsSource:
    """Configurable metrics source for testing canary analysis (ADR-046)."""

    def __init__(self, error_rate: float | None = 0.005, p95_latency_ms: float | None = 120.0):
        self.error_rate = error_rate
        self.p95_latency_ms = p95_latency_ms
        self.queries_executed: list[str] = []
        self.raise_error: Exception | None = None

    def query(self, promql: str) -> float | None:
        if self.raise_error is not None:
            raise self.raise_error
        self.queries_executed.append(promql)
        if "x{" in promql:
            return self.error_rate
        if "y{" in promql:
            return self.p95_latency_ms
        return None


@pytest.fixture
def fake_canary_metrics(monkeypatch):
    from backend.app import main as main_module

    fake = _FakeMetricsSource(error_rate=0.005, p95_latency_ms=120.0)
    spec = parse_verification({
        "queries": {
            "errorRate": 'sum(x{app="{release}",track="{track}"})',
            "p95LatencyMs": 'max(y{app="{release}"})',
        },
        "maxErrorRate": 0.05,
        "maxP95LatencyMs": 1000,
    })
    monkeypatch.setattr(main_module.portal, "verification_spec", lambda module_id: spec)
    monkeypatch.setattr(main_module, "metrics_source", fake)
    return fake


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


def _setup_canary_request(client, auth_headers, dev_headers, reviewer_headers, module_name="search-api", version_tag="v2.0.0"):
    sys_res = client.post(
        "/systems",
        headers=auth_headers,
        json={"id": f"sys-{uuid4().hex[:8]}", "unit": "Search", "description": "Canary test"},
    )
    assert sys_res.status_code == 201, sys_res.text
    system_id = sys_res.json()["id"]

    mod_name = f"{module_name}-{uuid4().hex[:6]}"
    mod_id, app_id, _ = _setup_verified_module(client, auth_headers, system_id, mod_name, version_tag)

    sched = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    req_res = client.post(
        "/production-requests",
        headers=dev_headers,
        json={
            "modules": [{"moduleId": mod_id, "version": version_tag}],
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

    appr_res = client.post(f"/production-requests/{req_id}/approve", headers=reviewer_headers, json={"comment": "approve canary"})
    assert appr_res.status_code == 202, appr_res.text
    dep_id = appr_res.json()["modules"][0]["deploymentId"]

    from backend.app.main import platform, portal
    canary_dep = platform.get_deployment(UUID(dep_id))
    platform.record_deployment_result(UUID(dep_id), "healthy", "canary up", fencing_token=canary_dep.fencing_token)
    portal.record_production_deployment_result(UUID(dep_id), "healthy", "canary up")

    return req_id, dep_id, mod_id


def test_canary_progressive_delivery_flow(client, auth_headers, dev_headers, reviewer_headers, fake_canary_metrics):
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
        json={},
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
    fake_canary_metrics.error_rate = 0.08
    fake_canary_metrics.p95_latency_ms = 150.0
    fail_adv = client.post(
        f"/production-requests/{req_id}/canary/advance",
        headers=reviewer_headers,
        json={},
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


def test_canary_last_step_promotes_the_stable_release(client, auth_headers, dev_headers, reviewer_headers, fake_canary_metrics):
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
        json={},
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


def test_canary_rules_that_cannot_be_applied_refuse_the_step(client, reviewer_headers, monkeypatch):
    """A failure to write the traffic split must not be reported as an advanced canary.

    This branch used to be `except Exception: pass`, so the step returned success while
    every request still went to stable -- the canary looked advanced and was not. The
    honest answer is a refusal naming the module the operator has to look at.
    """

    from backend.app import main as main_module

    monkeypatch.setattr(
        main_module.portal, "production_request",
        lambda request_id: {"deploymentId": str(uuid4()), "modules": [{"moduleId": "m-1"}]},
    )
    monkeypatch.setattr(
        main_module, "_canary_analysis",
        lambda *args, **kwargs: (VerificationResult(True, "ok", 1), ""),
    )

    def exploding_module(module_id):
        raise RuntimeError("projection unavailable")

    monkeypatch.setattr(main_module.portal, "module", exploding_module)

    response = client.post(
        "/production-requests/req-1/canary/advance",
        headers=reviewer_headers,
        json={"canaryRules": {"headerName": "x-canary", "headerValue": "yes"}},
    )

    assert response.status_code == 502, response.text
    body = response.json()
    assert body["code"] == "CANARY_RULES_NOT_APPLIED"
    # Naming the module is the point: an operator cannot act on "something failed".
    assert "m-1" in body["message"]


class _StubTransaction:
    """Just enough session for `start_release`: one request row, recorded writes."""

    def __init__(self, row):
        self.row = row
        self.updates: list[dict] = []
        self.audit: list = []

    def portal_request(self, request_id):
        return self.row

    def update_portal_request(self, request_id, *, status, comment, release_plan=None, **_):
        self.updates.append({"status": status, "comment": comment, "release_plan": release_plan})
        from dataclasses import replace
        self.row = replace(self.row, status=status, release_plan=release_plan or self.row.release_plan)

    def apply(self, unit):
        self.audit.extend(unit.audit)


def _release_plan_request(status="approved"):
    from datetime import datetime, timezone

    from backend.app.store.records import RequestRow

    return RequestRow(
        id="req-idem", modules=(), requested_by="dana",
        scheduled_for=datetime.now(timezone.utc), rollback_strategy="auto",
        run_automation_tests=False, status=status,
        release_plan={"waves": [{"moduleIds": [], "status": "pending"}]},
    )


def _coordinator_over(transaction):
    import contextlib

    from backend.app.coordinator import ReleasePlanCoordinator

    class _Portal:
        @contextlib.contextmanager
        def _session(self):
            yield transaction

    return ReleasePlanCoordinator(_Portal(), None)


def test_starting_a_release_twice_does_not_dispatch_wave_one_twice():
    """A retried approval, or a replayed callback, must not put one release out twice."""

    transaction = _StubTransaction(_release_plan_request())
    coordinator = _coordinator_over(transaction)

    first = coordinator.start_release("req-idem", "rae")
    assert not first.get("alreadyStarted")

    second = coordinator.start_release("req-idem", "rae")
    assert second["alreadyStarted"] is True
    assert second["dispatchedCount"] == 0
    # The second call must not have written the plan again.
    assert len(transaction.updates) == 1


def test_starting_a_release_is_audited():
    """The saga wrote nothing to the audit log, so a wave start left no trace at all."""

    transaction = _StubTransaction(_release_plan_request())
    _coordinator_over(transaction).start_release("req-idem", "rae")

    assert [record.event_type for record in transaction.audit] == ["release_plan.wave_started"]
    assert transaction.audit[0].actor == "rae"
    assert transaction.audit[0].payload["requestId"] == "req-idem"


def test_starting_a_release_does_not_force_the_status_back_to_approved():
    """`status="approved"` was written unconditionally, dragging any state back to approved."""

    transaction = _StubTransaction(_release_plan_request(status="cancelled"))
    _coordinator_over(transaction).start_release("req-idem", "rae")

    assert transaction.updates[0]["status"] == "cancelled"


def test_an_internal_canary_failure_is_not_reported_as_the_callers_mistake(client, reviewer_headers, monkeypatch):
    """Every exception used to become `400 CANARY_ERROR` with str(exc) in the body.

    Two things wrong with that. A database fault was reported to the operator as a bad
    request, so the one person who could escalate it was told to fix their own input.
    And `str(exc)` on a psycopg error carries SQL and table names out to the caller.
    A fault inside netCI is a 500 and its detail belongs in the log.
    """

    from backend.app import main as main_module
    from backend.app.coordinator import ReleasePlanCoordinator

    monkeypatch.setattr(
        main_module.portal, "production_request",
        lambda request_id: {"deploymentId": str(uuid4()), "modules": []},
    )
    monkeypatch.setattr(
        main_module, "_canary_analysis",
        lambda *args, **kwargs: (VerificationResult(True, "ok", 1), ""),
    )

    def exploding(self, *args, **kwargs):
        raise RuntimeError('relation "production_requests" does not exist')

    monkeypatch.setattr(ReleasePlanCoordinator, "advance_canary", exploding)

    response = client.post("/production-requests/req-1/canary/advance",
                           headers=reviewer_headers, json={})

    assert response.status_code == 500, response.text
    assert response.json()["code"] == "CANARY_INTERNAL_ERROR"
    assert "production_requests" not in response.text


def test_a_caller_mistake_on_the_canary_is_still_a_400(client, reviewer_headers, monkeypatch):
    """The coordinator's ValueErrors are written for the caller, and stay 400."""

    from backend.app import main as main_module
    from backend.app.coordinator import ReleasePlanCoordinator

    monkeypatch.setattr(
        main_module.portal, "production_request",
        lambda request_id: {"deploymentId": str(uuid4()), "modules": []},
    )
    monkeypatch.setattr(
        main_module, "_canary_analysis",
        lambda *args, **kwargs: (VerificationResult(True, "ok", 1), ""),
    )

    def refuses(self, *args, **kwargs):
        raise ValueError("canary delivery needs the kubernetes runtime; module m-1 runs on docker")

    monkeypatch.setattr(ReleasePlanCoordinator, "advance_canary", refuses)

    response = client.post("/production-requests/req-1/canary/advance",
                           headers=reviewer_headers, json={})

    assert response.status_code == 400, response.text
    assert response.json()["code"] == "CANARY_ERROR"
    assert "kubernetes runtime" in response.json()["message"]


def test_canary_advance_refuses_caller_metrics(client, auth_headers, dev_headers, reviewer_headers, fake_canary_metrics):
    req_id, dep_id, _ = _setup_canary_request(client, auth_headers, dev_headers, reviewer_headers, "canary-caller-metrics")
    response = client.post(
        f"/production-requests/{req_id}/canary/advance",
        headers=reviewer_headers,
        json={"metrics": {"errorRate": 0.01}},
    )
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "CANARY_METRICS_ARE_READ_BY_NETCI"

    traffic = client.get(f"/deployments/{dep_id}/traffic", headers=auth_headers).json()
    assert traffic["trafficWeight"] == 10


def test_canary_advance_refuses_when_no_verification_spec_and_no_override(
    client, auth_headers, dev_headers, reviewer_headers, monkeypatch, fake_canary_metrics
):
    from backend.app import main as main_module

    monkeypatch.setattr(main_module.portal, "verification_spec", lambda module_id: None)

    req_id, dep_id, _ = _setup_canary_request(client, auth_headers, dev_headers, reviewer_headers, "canary-no-spec")
    response = client.post(
        f"/production-requests/{req_id}/canary/advance",
        headers=reviewer_headers,
        json={},
    )
    assert response.status_code == 409, response.text
    body = response.json()
    assert body["code"] == "CANARY_ANALYSIS_UNAVAILABLE"
    assert "pipelineConfig.verification" in body["message"]

    traffic = client.get(f"/deployments/{dep_id}/traffic", headers=auth_headers).json()
    assert traffic["trafficWeight"] == 10


def test_canary_advance_fails_409_when_prometheus_returns_none_and_not_aborted(
    client, auth_headers, dev_headers, reviewer_headers, fake_canary_metrics
):
    from backend.app import main as main_module

    fake_canary_metrics.error_rate = None
    req_id, dep_id, _ = _setup_canary_request(client, auth_headers, dev_headers, reviewer_headers, "canary-prom-none")
    status_before = client.get(f"/production-requests/{req_id}/plan", headers=auth_headers).json()["status"]

    response = client.post(
        f"/production-requests/{req_id}/canary/advance",
        headers=reviewer_headers,
        json={},
    )
    assert response.status_code == 409, response.text
    body = response.json()
    assert body["code"] == "CANARY_ANALYSIS_UNAVAILABLE"
    assert "Prometheus returned no data" in body["message"]

    # Status of the request/deployment unchanged and not aborted
    plan = client.get(f"/production-requests/{req_id}/plan", headers=auth_headers).json()
    assert plan["status"] == status_before

    dep = main_module.platform.get_deployment(UUID(dep_id))
    assert dep.status.value == "healthy"
    assert dep.traffic_weight == 10


def test_canary_advance_fails_409_when_metrics_unavailable(
    client, auth_headers, dev_headers, reviewer_headers, fake_canary_metrics
):
    fake_canary_metrics.raise_error = MetricsUnavailable("down")
    req_id, dep_id, _ = _setup_canary_request(client, auth_headers, dev_headers, reviewer_headers, "canary-down")

    response = client.post(
        f"/production-requests/{req_id}/canary/advance",
        headers=reviewer_headers,
        json={},
    )
    assert response.status_code == 409, response.text
    body = response.json()
    assert body["code"] == "CANARY_ANALYSIS_UNAVAILABLE"
    assert "down" in body["message"]


def test_canary_advance_with_override_reason_when_no_spec(
    client, auth_headers, dev_headers, reviewer_headers, monkeypatch, fake_canary_metrics
):
    from backend.app import main as main_module

    monkeypatch.setattr(main_module.portal, "verification_spec", lambda module_id: None)

    req_id, dep_id, _ = _setup_canary_request(client, auth_headers, dev_headers, reviewer_headers, "canary-override")

    response = client.post(
        f"/production-requests/{req_id}/canary/advance",
        headers=reviewer_headers,
        json={"overrideReason": "manual verification completed by team lead"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["allowed"] is True
    assert body["status"] == "advanced"
    assert body["analysed"] is False
    assert body["reason"].startswith("advanced WITHOUT analysis")

    if hasattr(main_module.platform, "audit_records"):
        records = main_module.platform.audit_records()
    else:
        with main_module.database.transaction() as tx:
            records, _, _ = tx.audit_records_paginated(limit=100)

    override_records = [r for r in records if r.event_type == "canary.advanced_without_analysis"]
    assert len(override_records) >= 1
    assert any(
        r.payload.get("productionRequestId") == req_id
        and r.payload.get("reason") == "manual verification completed by team lead"
        for r in override_records
    )


def test_canary_advance_refuses_override_reason_shorter_than_10_chars(
    client, auth_headers, dev_headers, reviewer_headers, monkeypatch, fake_canary_metrics
):
    from backend.app import main as main_module

    monkeypatch.setattr(main_module.portal, "verification_spec", lambda module_id: None)

    req_id, dep_id, _ = _setup_canary_request(client, auth_headers, dev_headers, reviewer_headers, "canary-short-reason")
    response = client.post(
        f"/production-requests/{req_id}/canary/advance",
        headers=reviewer_headers,
        json={"overrideReason": "too short"},
    )
    assert response.status_code == 422, response.text


def test_canary_promql_query_contains_module_and_track(
    client, auth_headers, dev_headers, reviewer_headers, fake_canary_metrics
):
    req_id, dep_id, mod_id = _setup_canary_request(client, auth_headers, dev_headers, reviewer_headers, "canary-promql-check")

    response = client.post(
        f"/production-requests/{req_id}/canary/advance",
        headers=reviewer_headers,
        json={},
    )
    assert response.status_code == 200, response.text
    assert any(f'app="{mod_id}"' in q and 'track="canary"' in q for q in fake_canary_metrics.queries_executed)

