"""Service scorecards and delivery insights: read-only projections over stored data.

Every check and every metric here has to say what it is based on, and anything netCI
cannot evaluate from what actually happened is `None` ("unknown"), never a pass and
never a guess. These tests drive the projections both directly (for the pure pieces:
failure classification, the flaky-build heuristic, percentiles) and through the API
(for the scorecard and insights routes, including visibility).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

import app.main as main_mod
from app.auth import Principal, Role
from app.domain.models import Environment, PipelineRun, PipelineStage, PipelineStatus
from app.projections.insights import _flaky, _nearest_rank, classify_failure
from app.store.records import ArtifactRescanRecord
from toolchain_report import declared_tool_report

client = TestClient(main_mod.app)
MACHINE = {"Authorization": "Bearer netci-local-pipeline-key"}


@pytest.fixture(autouse=True)
def fresh_platform():
    # Other test modules reload app.main, which leaves an import-time client on a stale
    # app: a dependency override set on main_mod.app would then never reach it.
    global client
    client = TestClient(main_mod.app)
    main_mod.platform.reset()
    yield


def _run(*, status=PipelineStatus.QUEUED, commit_sha="a" * 40, retry_of=None) -> PipelineRun:
    return PipelineRun(
        application_id=uuid4(),
        commit_sha=commit_sha,
        environment=Environment.DEV,
        status=status,
        retry_of=retry_of,
    )


def _stage(run_id, stage_id, status, *, error_message=None) -> PipelineStage:
    return PipelineStage(
        pipeline_run_id=run_id,
        stage_id=stage_id,
        stage_name=stage_id,
        status=status,
        error_message=error_message,
    )


def _create_module(name: str, *, environments=("dev", "staging", "prod"), owner_team=None) -> dict:
    system_id = f"sys-{uuid4().hex[:6]}"
    assert client.post(
        "/systems", json={"id": system_id, "unit": "Scorecards", "description": "test system"}
    ).status_code == 201
    body = {
        "name": name,
        "displayName": name,
        "repositoryUrl": f"https://github.com/acme/{name}",
        "pipelineTemplate": "container-ci-cd-v1",
        "runtime": "docker",
        "moduleType": "Backend",
        "description": "test module",
        "deploymentEnvironments": [
            {"displayName": env, "environment": env, "runtime": "docker", "servers": [f"{env}-host"]}
            for env in environments
        ],
    }
    if owner_team is not None:
        body["ownerTeam"] = owner_team
    created = client.post(f"/systems/{system_id}/modules", json=body)
    assert created.status_code == 201, created.text
    return created.json()


# --------------------------------------------------------------- classify_failure


def test_classify_failure_infrastructure_beats_stage_based():
    # A "build" stage failed, but the log line says the real story is infrastructure --
    # infrastructure must win even though the stage looks like an ordinary build failure.
    run = _run()
    stages = [_stage(run.id, "build", "failed")]
    logs = ["Connection refused while pulling base image"]
    assert classify_failure(run, stages, logs) == "infrastructure"


@pytest.mark.parametrize(
    "signal",
    [
        "no space left on device",
        "too many open files",
        "CONNECTION REFUSED",
        "network is unreachable",
        "could not resolve host",
        "operation timed out",
        "TIMEOUT talking to agent",
        "agent went offline",
        "pod evicted",
        "reconciled: false",
    ],
)
def test_classify_failure_infrastructure_signals(signal):
    run = _run()
    assert classify_failure(run, [], [signal]) == "infrastructure"


def test_classify_failure_tests():
    run = _run()
    stages = [_stage(run.id, "unit-test", "failed", error_message="3 tests failed")]
    assert classify_failure(run, stages, []) == "tests"


def test_classify_failure_build():
    run = _run()
    stages = [_stage(run.id, "build", "failed")]
    assert classify_failure(run, stages, ["compiler error"]) == "build"


def test_classify_failure_security_from_stage():
    run = _run()
    stages = [_stage(run.id, "vulnerability-scan", "failed")]
    assert classify_failure(run, stages, []) == "security"


def test_classify_failure_security_from_log_line():
    run = _run()
    stages = [_stage(run.id, "checkout", "failed")]
    assert classify_failure(run, stages, ["ARTIFACT_POLICY_DENIED: critical CVE"]) == "security"


def test_classify_failure_supply_chain_from_stage():
    run = _run()
    stages = [_stage(run.id, "sign", "failed")]
    assert classify_failure(run, stages, []) == "supply-chain"


def test_classify_failure_supply_chain_from_log_line():
    run = _run()
    stages = [_stage(run.id, "checkout", "failed")]
    assert classify_failure(run, stages, ["no provenance attestation found"]) == "supply-chain"


def test_classify_failure_deploy_from_stage():
    run = _run()
    stages = [_stage(run.id, "deploy", "failed")]
    assert classify_failure(run, stages, []) == "deploy"


def test_classify_failure_deploy_from_log_line():
    run = _run()
    stages = [_stage(run.id, "checkout", "failed")]
    assert classify_failure(run, stages, ["deployment failed: readiness probe never passed"]) == "deploy"


def test_classify_failure_unknown_when_nothing_matches():
    run = _run()
    stages = [_stage(run.id, "checkout", "failed", error_message="unexpected exit code 17")]
    assert classify_failure(run, stages, ["exit code 17"]) == "unknown"


def test_classify_failure_unknown_with_no_failed_stage_and_no_signal():
    run = _run()
    stages = [_stage(run.id, "checkout", "succeeded")]
    assert classify_failure(run, stages, ["all good"]) == "unknown"


# --------------------------------------------------------------------- flaky


def test_flaky_detects_failed_then_succeeded_same_commit_via_retry_chain():
    failed = _run(status=PipelineStatus.FAILED, commit_sha="c" * 40)
    passed = _run(status=PipelineStatus.SUCCEEDED, commit_sha="c" * 40, retry_of=failed.id)
    result = _flaky({failed.id: failed, passed.id: passed}, [failed, passed])
    assert result == {
        "count": 1,
        "commits": [{"commitSha": "c" * 40, "failedRunId": str(failed.id), "passedRunId": str(passed.id)}],
    }


def test_flaky_a_different_commit_is_not_flaky():
    # The retry names a parent that failed, but on a different commit -- a rebase or a
    # fixup between attempts, not the same change succeeding on a second try.
    failed = _run(status=PipelineStatus.FAILED, commit_sha="d" * 40)
    passed = _run(status=PipelineStatus.SUCCEEDED, commit_sha="e" * 40, retry_of=failed.id)
    result = _flaky({failed.id: failed, passed.id: passed}, [failed, passed])
    assert result == {"count": 0, "commits": []}


def test_flaky_failed_then_failed_is_not_flaky():
    first = _run(status=PipelineStatus.FAILED, commit_sha="f" * 40)
    second = _run(status=PipelineStatus.FAILED, commit_sha="f" * 40, retry_of=first.id)
    result = _flaky({first.id: first, second.id: second}, [first, second])
    assert result == {"count": 0, "commits": []}


# ---------------------------------------------------------------- nearest-rank percentiles


def test_nearest_rank_no_samples_is_null():
    assert _nearest_rank([], 50) is None
    assert _nearest_rank([], 95) is None


def test_nearest_rank_one_sample():
    assert _nearest_rank([7.0], 50) == 7.0
    assert _nearest_rank([7.0], 95) == 7.0


def test_nearest_rank_two_samples():
    values = [1.0, 3.0]
    assert _nearest_rank(values, 50) == 1.0
    assert _nearest_rank(values, 95) == 3.0


def test_nearest_rank_ten_samples():
    values = [float(i) for i in range(1, 11)]  # 1..10
    assert _nearest_rank(values, 50) == 5.0
    assert _nearest_rank(values, 95) == 10.0


# ---------------------------------------------------------------------- scorecard: fresh module


def test_scorecard_for_a_fresh_module_is_mostly_unknown_not_a_pass():
    module = _create_module("fresh-svc", owner_team="team-alpha")

    resp = client.get(f"/modules/{module['id']}/scorecard")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["moduleId"] == module["id"]

    checks = {c["id"]: c for c in data["checks"]}
    assert set(checks) == {
        "owner", "delivery-rules", "verification", "staging-before-prod", "provenance",
        "sbom-in-service", "rescanned-recently", "no-critical-running", "recent-success",
        "change-fail-rate",
    }
    assert checks["owner"]["passed"] is True
    assert "team-alpha" in checks["owner"]["detail"]
    assert checks["delivery-rules"]["passed"] is False  # nothing declared -- defaults apply
    assert checks["verification"]["passed"] is False
    assert checks["staging-before-prod"]["passed"] is False  # no promotion rule configured
    # Nothing has run or deployed yet: these have no basis to evaluate and must be null,
    # never a false pass masquerading as "clean".
    assert checks["provenance"]["passed"] is None
    assert checks["sbom-in-service"]["passed"] is None
    assert checks["rescanned-recently"]["passed"] is None
    assert checks["no-critical-running"]["passed"] is None
    assert checks["change-fail-rate"]["passed"] is None
    assert checks["recent-success"]["passed"] is False

    assert data["score"]["total"] == 10
    assert data["score"]["known"] < data["score"]["total"]
    assert data["score"]["passed"] == 1


def test_scorecard_404_for_unknown_module():
    resp = client.get("/modules/does-not-exist/scorecard")
    assert resp.status_code == 404


# ------------------------------------------------------- scorecard: built, deployed, scanned


def _build_deploy_and_scan(module: dict, *, digest: str) -> None:
    """Build with verified provenance, deploy to dev, upload an SBOM, and fake a rescan.

    Mirrors backend/tests/test_vulnerability_exposure.py's `_setup_in_service_module`:
    CI events go through `platform` directly (this is what Jenkins calls in production),
    while the SBOM upload and ci-result/evidence calls go through the HTTP API the way a
    real pipeline reaches it.
    """

    run = main_mod.platform.start_pipeline(
        UUID(module["applicationId"]),
        commit_sha="b" * 40,
        branch="main",
        environment=Environment.DEV,
        parameters={},
        correlation_id=f"corr-{uuid4().hex[:6]}",
        idempotency_key=f"start-{uuid4().hex[:6]}",
    )
    assert client.post(f"/pipeline-runs/{run.id}/ci-result", headers=MACHINE, json={"status": "running"}).status_code == 202

    evidence = {
        "artifactDigest": digest,
        "artifactRef": f"registry.local/{module['name']}@{digest}",
        "sbom": {"generatedBy": "syft", "location": "s3://evidence/sbom.json", "format": "cyclonedx-json"},
        "vulnerabilityScan": {"scanner": "trivy", "status": "passed", "critical": 0, "high": 0, "findings": []},
        "signature": {"provider": "cosign", "verified": True, "certificateIdentity": "netci"}, "toolVersions": declared_tool_report(),
        "provenance": {
            "predicateType": "https://slsa.dev/provenance/v1",
            "verified": True,
            "repository": module["repositoryUrl"],
            "commit": "b" * 40,
        },
    }
    ev_resp = client.post(f"/pipeline-runs/{run.id}/security-evidence", headers=MACHINE, json=evidence)
    assert ev_resp.status_code == 202, ev_resp.text

    ci_resp = client.post(
        f"/pipeline-runs/{run.id}/ci-result", headers=MACHINE, json={"status": "succeeded", "artifactDigest": digest}
    )
    assert ci_resp.status_code == 202, ci_resp.text
    deployment_id = ci_resp.json()["deployment"]["id"]
    dep_resp = client.post(
        f"/deployments/{deployment_id}/result", headers=MACHINE, json={"status": "healthy", "message": "ok"}
    )
    assert dep_resp.status_code == 202, dep_resp.text

    sbom = {
        "bomFormat": "CycloneDX", "specVersion": "1.4", "version": 1,
        "components": [{"name": "openssl", "version": "1.1.1t", "type": "library"}],
    }
    sbom_resp = client.post(f"/pipeline-runs/{run.id}/sbom", headers=MACHINE, json=sbom)
    assert sbom_resp.status_code == 202, sbom_resp.text

    now = datetime.now(timezone.utc)
    with main_mod.database.transaction() as tx:
        tx.record_artifact_rescan(ArtifactRescanRecord(digest, now, "scanned", "trivy", "0 findings"))


def test_scorecard_after_build_deploy_sbom_and_rescan():
    module = _create_module("scanned-svc", environments=("dev",))
    digest = "sha256:" + "9" * 64
    _build_deploy_and_scan(module, digest=digest)

    resp = client.get(f"/modules/{module['id']}/scorecard")
    assert resp.status_code == 200, resp.text
    checks = {c["id"]: c for c in resp.json()["checks"]}

    assert checks["provenance"]["passed"] is True
    assert checks["sbom-in-service"]["passed"] is True
    assert checks["rescanned-recently"]["passed"] is True


# ----------------------------------------------------------------------- /scorecards visibility

VIEWER_ALPHA = Principal(
    subject="viewer-alpha",
    display_name="Viewer Alpha",
    email="alpha@example.com",
    roles=frozenset({Role.VIEWER}),
    method="token",
    teams=frozenset({"team-alpha"}),
)


def test_scorecards_endpoint_only_lists_visible_modules():
    visible_module = _create_module("visible-svc", owner_team="team-alpha")
    hidden_module = _create_module("hidden-svc", owner_team="team-beta")

    # Unscoped (default test principal is platform-admin-equivalent): both are visible.
    all_resp = client.get("/scorecards")
    assert all_resp.status_code == 200
    all_ids = {item["moduleId"] for item in all_resp.json()["items"]}
    assert {visible_module["id"], hidden_module["id"]} <= all_ids

    main_mod.app.dependency_overrides[main_mod.current_principal] = lambda: VIEWER_ALPHA
    try:
        scoped_resp = client.get("/scorecards")
        assert scoped_resp.status_code == 200
        scoped_ids = {item["moduleId"] for item in scoped_resp.json()["items"]}
        assert visible_module["id"] in scoped_ids
        assert hidden_module["id"] not in scoped_ids
    finally:
        main_mod.app.dependency_overrides.pop(main_mod.current_principal, None)


# -------------------------------------------------------------------------------- insights


def test_insights_endpoint_classifies_a_failed_run_and_measures_queue_time():
    module = _create_module("insights-svc", environments=("dev",))
    app_id = UUID(module["applicationId"])
    run = main_mod.platform.start_pipeline(
        app_id,
        commit_sha="1" * 40,
        branch="main",
        environment=Environment.DEV,
        parameters={},
        correlation_id=f"corr-{uuid4().hex[:6]}",
        idempotency_key=f"start-{uuid4().hex[:6]}",
    )
    checkout_started = run.created_at + timedelta(seconds=4)
    main_mod.platform.record_stage_event(
        run.id, stage_id="checkout", stage_name="Checkout", status="succeeded", started_at=checkout_started,
    )
    main_mod.platform.record_stage_event(
        run.id, stage_id="unit-test", stage_name="Unit Test", status="failed", error_message="2 tests failed",
    )
    assert client.post(f"/pipeline-runs/{run.id}/ci-result", headers=MACHINE, json={"status": "running"}).status_code == 202
    assert client.post(f"/pipeline-runs/{run.id}/ci-result", headers=MACHINE, json={"status": "failed"}).status_code == 202

    resp = client.get(f"/modules/{module['id']}/insights", params={"days": 30})
    assert resp.status_code == 200, resp.text
    data = resp.json()

    assert data["failures"]["total"] == 1
    assert data["failures"]["byClass"] == {"tests": 1}
    assert data["failures"]["recent"][0]["pipelineRunId"] == str(run.id)
    assert data["failures"]["recent"][0]["class"] == "tests"
    assert data["failures"]["recent"][0]["stage"] == "unit-test"

    assert data["queueTime"]["samples"] == 1
    assert data["queueTime"]["p50Seconds"] == pytest.approx(4.0, abs=0.5)
    assert data["queueTime"]["p95Seconds"] == pytest.approx(4.0, abs=0.5)

    # Nothing retried and nothing deployed in this scenario.
    assert data["flaky"] == {"count": 0, "commits": []}
    assert data["leadTime"] == {"samples": 0, "meanSeconds": None}


def test_insights_days_out_of_range_is_422():
    module = _create_module("bounds-svc", environments=("dev",))
    assert client.get(f"/modules/{module['id']}/insights", params={"days": 0}).status_code == 422
    assert client.get(f"/modules/{module['id']}/insights", params={"days": 366}).status_code == 422


def test_insights_404_for_unknown_module():
    resp = client.get("/modules/does-not-exist/insights")
    assert resp.status_code == 404
