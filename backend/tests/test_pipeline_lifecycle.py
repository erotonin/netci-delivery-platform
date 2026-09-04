"""Tests for Pipeline Lifecycle, Cancellation, Retry, Stage Events, and Reconciler.

Phase 7 (P1.2):
- Pipeline cancellation wires CI launcher abort and records audit events.
- Deployment cancellation wires CD orchestrator cancel, releases lease, records audit events.
- Pipeline retry creates a new run with retry_of lineage while keeping original run immutable.
- Granular stage callbacks enforce workload identity scopes (ci:stage / ci:result) and persist stages.
- Reconciler discovers lost callbacks, repairs state atomically, and logs reconciliation audits.
"""

from __future__ import annotations

import os
from dataclasses import replace
from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app import workload_identity
from app.adapters.cd_orchestrator import CdOrchestrator, NullCdOrchestrator
from app.adapters.ci_launcher import CiLauncher, LaunchedCi, NullCiLauncher
from app.domain.models import (
    Application,
    Deployment,
    DeploymentStatus,
    Environment,
    PipelineRun,
    PipelineStage,
    PipelineStatus,
    Runtime,
)
from app.main import app
from app.persistence import UnitOfWork
from app.reconciler import Reconciler
from app.workload_identity import Scope, Workload

client = TestClient(app)
KEYS = "k1:" + "z" * 48
DATABASE_URL = os.getenv("NETCI_TEST_DATABASE_URL", "").strip()


@pytest.fixture(autouse=True)
def reset_environment(monkeypatch):
    monkeypatch.setenv("NETCI_WORKLOAD_TOKEN_KEYS", KEYS)
    monkeypatch.delenv("NETCI_WORKLOAD_TOKEN_KEYS_FILE", raising=False)
    main.platform.reset()
    main.portal.reset()


def create_application(name: str = "lifecycle-app", runtime: str = "docker") -> dict:
    res = client.post(
        "/applications",
        json={
            "name": name,
            "repositoryUrl": f"https://github.com/example/{name}",
            "pipelineTemplate": "container-ci-cd-v1",
            "runtime": runtime,
            "defaultEnvironment": "staging",
        },
    )
    assert res.status_code == 201, res.text
    return res.json()


def trigger_run(app_id: str, environment: str = "staging", commit_sha: str = "1234567890abcdef") -> dict:
    res = client.post(
        f"/applications/{app_id}/pipeline-runs",
        json={
            "commitSha": commit_sha,
            "branch": "main",
            "environment": environment,
            "parameters": {},
        },
    )
    assert res.status_code == 202, res.text
    return res.json()


def mint_ci_token(app_id: UUID, run_id: UUID, scopes=None) -> str:
    return workload_identity.mint(
        workload=Workload.JENKINS,
        application_id=app_id,
        pipeline_run_id=run_id,
        scopes=scopes or {Scope.CI_STAGE, Scope.CI_RESULT, Scope.CI_EVIDENCE},
        ttl_seconds=3600,
    )


# ==============================================================================
# 1. Pipeline Run Cancellation
# ==============================================================================


def test_cancel_pipeline_run_aborts_ci_and_transitions_to_cancelled(monkeypatch):
    aborted_runs = []

    class MockCiLauncher(CiLauncher):
        def launch(self, *args, **kwargs):
            return LaunchedCi(controller_id="ctrl-1", external_run_id="99")

        def abort(self, run_id: str):
            aborted_runs.append(run_id)

        def get_status(self, run_id: str):
            return "running"

    monkeypatch.setattr(main.platform, "ci_launcher", MockCiLauncher())

    app_data = create_application("cancel-app")
    run_data = trigger_run(app_data["id"])
    run_id = run_data["id"]

    # Cancel the running pipeline
    res = client.post(
        f"/pipeline-runs/{run_id}/cancel",
        json={"reason": "User requested cancellation"},
        headers={"X-Correlation-Id": "cancel-corr-1"},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "cancelled"
    assert "ctrl-1:99" in aborted_runs or run_id in aborted_runs

    # Verify query returns cancelled status
    get_res = client.get(f"/pipeline-runs/{run_id}")
    assert get_res.status_code == 200
    assert get_res.json()["status"] == "cancelled"

    # Verify audit event was logged in store
    with main.platform._transaction() as s:
        records = s.audit_records()
    events = [r for r in records if r.event_type == "pipeline.cancelled" and str(r.pipeline_run_id) == run_id]
    assert len(events) == 1
    assert events[0].payload["reason"] == "User requested cancellation"


def test_cancel_terminal_pipeline_run_is_rejected():
    app_data = create_application("terminal-cancel-app")
    run_data = trigger_run(app_data["id"])
    run_id = run_data["id"]

    # Mark run as failed in database via UnitOfWork
    with main.platform._transaction() as s:
        run = s.pipeline_run(UUID(run_id))
        failed_run = replace(run, status=PipelineStatus.FAILED, version=run.version + 1)
        unit = UnitOfWork(runs=[(failed_run, run.version)])
        s.apply(unit)

    # Cancel on terminal run -> 409 Conflict
    res2 = client.post(f"/pipeline-runs/{run_id}/cancel", json={"reason": "Cannot cancel terminal"})
    assert res2.status_code == 409
    assert res2.json()["code"] == "INVALID_PIPELINE_STATE"


# ==============================================================================
# 2. Deployment Cancellation
# ==============================================================================


def test_cancel_deployment_aborts_temporal_and_releases_lease(monkeypatch):
    app_data = create_application("cancel-deploy-app")
    app_id = UUID(app_data["id"])

    cancelled_workflows = []

    class MockCdOrchestrator(CdOrchestrator):
        def start_deployment(self, **kwargs):
            return "wf-1"

        def cancel(self, workflow_id: str, reason: str = ""):
            cancelled_workflows.append((workflow_id, reason))

        def get_status(self, workflow_id: str):
            return "running"

    monkeypatch.setattr(main.platform, "cd_orchestrator", MockCdOrchestrator())

    # Create a deployment in deploying state with an active lease
    dep_id = uuid4()
    dep = Deployment(
        id=dep_id,
        application_id=app_id,
        pipeline_run_id=None,
        runtime=Runtime.DOCKER,
        environment=Environment.STAGING,
        status=DeploymentStatus.DEPLOYING,
        artifact_digest="sha256:" + "a" * 64,
        fencing_token=1,
    )
    with main.platform._transaction() as s:
        unit = UnitOfWork(deployments=[(dep, None)])
        s.apply(unit)
        main.platform._acquire_lease(s, unit, dep, None, owner="test-dep")

    # Cancel the deployment
    res = client.post(
        f"/deployments/{dep_id}/cancel",
        json={"reason": "Rollout degraded in staging"},
        headers={"X-Correlation-Id": "dep-cancel-corr"},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "cancelled"
    assert len(cancelled_workflows) == 1

    # Verify lease was released
    target = main.platform.lease_target(dep, None)
    with main.platform._transaction() as s:
        lease = s.active_deployment_lease(application_id=app_id, environment=Environment.STAGING.value, target=target)
        assert lease is None

    # Verify audit event in store
    with main.platform._transaction() as s:
        records = s.audit_records()
    events = [r for r in records if r.event_type == "deployment.cancelled" and r.deployment_id == dep_id]
    assert len(events) == 1
    assert events[0].payload["reason"] == "Rollout degraded in staging"


# ==============================================================================
# 3. Pipeline Run Retry
# ==============================================================================


def test_retry_pipeline_run_preserves_original_immutability_and_tracks_lineage(monkeypatch):
    launched_calls = []

    class MockCiLauncher(CiLauncher):
        def launch(self, *args, **kwargs):
            launched_calls.append((args, kwargs))
            return LaunchedCi(controller_id="ctrl-retry", external_run_id="42")

        def abort(self, run_id: str):
            pass

        def get_status(self, run_id: str):
            return "running"

    monkeypatch.setattr(main.platform, "ci_launcher", MockCiLauncher())

    app_data = create_application("retry-app")
    app_id = app_data["id"]
    original_run = trigger_run(app_id, commit_sha="abcdef1234567890")
    original_run_id = original_run["id"]

    # Transition original run to CANCELLED
    res = client.post(f"/pipeline-runs/{original_run_id}/cancel", json={"reason": "Build broke"})
    assert res.status_code == 200

    # Retry the run
    retry_res = client.post(f"/pipeline-runs/{original_run_id}/retry")
    assert retry_res.status_code == 201, retry_res.text
    retry_data = retry_res.json()

    # Verify retry run properties
    new_run_id = retry_data["id"]
    assert new_run_id != original_run_id
    assert retry_data["retryOf"] == original_run_id
    assert retry_data["commitSha"] == "abcdef1234567890"
    assert retry_data["environment"] == "staging"
    assert retry_data["status"] in ("running", "queued")
    assert len(launched_calls) == 2  # 1 for initial trigger + 1 for retry

    # Verify original run remains completely immutable
    orig_check = client.get(f"/pipeline-runs/{original_run_id}").json()
    assert orig_check["id"] == original_run_id
    assert orig_check["status"] == "cancelled"
    assert orig_check.get("retryOf") is None

    # Verify audit event logged in store
    with main.platform._transaction() as s:
        records = s.audit_records()
    events = [r for r in records if r.event_type == "pipeline.retried" and str(r.pipeline_run_id) == new_run_id]
    assert len(events) == 1
    assert events[0].payload["parentRunId"] == original_run_id


def test_retry_running_pipeline_is_rejected():
    app_data = create_application("retry-conflict-app")
    run = trigger_run(app_data["id"])
    run_id = run["id"]

    # Run is currently running/queued -> retry must return 409
    res = client.post(f"/pipeline-runs/{run_id}/retry")
    assert res.status_code == 409
    assert res.json()["code"] == "RUN_STILL_ACTIVE"


# ==============================================================================
# 4. Granular Stage Events and Scoped Callbacks
# ==============================================================================


def test_stage_events_unauthorized_token_rejected():
    app_data = create_application("stage-auth-app")
    run = trigger_run(app_data["id"])
    run_id = run["id"]

    # No auth header
    res = client.post(
        f"/pipeline-runs/{run_id}/stages",
        json={"stageId": "build", "stageName": "Build Docker", "status": "running"},
    )
    assert res.status_code == 401

    # Token with wrong scope (e.g. only DEPLOYMENT_RESULT)
    wrong_token = workload_identity.mint(
        workload=Workload.TEMPORAL,
        application_id=UUID(app_data["id"]),
        deployment_id=uuid4(),
        scopes={Scope.DEPLOYMENT_RESULT},
    )
    res = client.post(
        f"/pipeline-runs/{run_id}/stages",
        json={"stageId": "build", "stageName": "Build Docker", "status": "running"},
        headers={"Authorization": f"Bearer {wrong_token}"},
    )
    assert res.status_code == 403


def test_stage_events_lifecycle_and_retrieval():
    app_data = create_application("stage-lifecycle-app")
    run = trigger_run(app_data["id"])
    run_id = run["id"]
    token = mint_ci_token(UUID(app_data["id"]), UUID(run_id), scopes={Scope.CI_STAGE})
    auth_header = {"Authorization": f"Bearer {token}"}

    # Record stage queued & running
    now_iso = datetime.now(timezone.utc).isoformat()
    stage1 = client.post(
        f"/pipeline-runs/{run_id}/stages",
        json={
            "stageId": "compile",
            "stageName": "Compile and Unit Test",
            "attempt": 1,
            "status": "running",
            "queuedAt": now_iso,
            "startedAt": now_iso,
        },
        headers=auth_header,
    )
    assert stage1.status_code == 202, stage1.text
    assert stage1.json()["status"] == "running"

    # Query stages
    stages_res = client.get(f"/pipeline-runs/{run_id}/stages")
    assert stages_res.status_code == 200
    items = stages_res.json()["items"]
    assert len(items) == 1
    assert items[0]["stageId"] == "compile"
    assert items[0]["status"] == "running"

    # Complete stage via path /stages/{stageId}
    stage1_done = client.post(
        f"/pipeline-runs/{run_id}/stages/compile",
        json={
            "status": "succeeded",
            "attempt": 1,
            "completedAt": datetime.now(timezone.utc).isoformat(),
            "durationMs": 5400,
            "logSnippet": "Tests: 42 passed, 0 failed\nArtifact written",
        },
        headers=auth_header,
    )
    assert stage1_done.status_code == 202, stage1_done.text
    assert stage1_done.json()["status"] == "succeeded"
    assert stage1_done.json()["durationMs"] == 5400
    assert "42 passed" in stage1_done.json()["logSnippet"]

    # Record second stage
    stage2 = client.post(
        f"/pipeline-runs/{run_id}/stages",
        json={
            "stageId": "security_scan",
            "stageName": "SAST & Container Scan",
            "attempt": 1,
            "status": "succeeded",
            "durationMs": 3200,
        },
        headers=auth_header,
    )
    assert stage2.status_code == 202

    # Query stages again
    stages_res2 = client.get(f"/pipeline-runs/{run_id}/stages")
    assert stages_res2.status_code == 200
    items2 = stages_res2.json()["items"]
    assert len(items2) == 2
    assert {s["stageId"] for s in items2} == {"compile", "security_scan"}


# ==============================================================================
# 5. Reconciler Watchdog Service
# ==============================================================================


def test_reconciler_repairs_lost_ci_callback(monkeypatch):
    app_data = create_application("reconcile-ci-app")
    run = trigger_run(app_data["id"])
    run_id = run["id"]

    # Jenkins finished with FAILURE, but webhook was lost
    class MockCiLauncher(CiLauncher):
        def launch(self, *args, **kwargs):
            return LaunchedCi(controller_id="ctrl-rec", external_run_id="rec-1")

        def abort(self, run_id: str):
            pass

        def get_status(self, r_id: str):
            return "failed"

    reconciler = Reconciler(
        platform=main.platform,
        ci_launcher=MockCiLauncher(),
        cd_orchestrator=NullCdOrchestrator(),
        default_timeout_seconds=3600,
    )

    reconciled = reconciler.reconcile_runs()
    assert len(reconciled) == 1
    assert reconciled[0]["pipelineRunId"] == run_id
    assert reconciled[0]["action"] == "reconciled_failed"

    # Verify run updated in database
    updated_run = client.get(f"/pipeline-runs/{run_id}").json()
    assert updated_run["status"] == "failed"

    # Verify audit event in store
    with main.platform._transaction() as s:
        records = s.audit_records()
    events = [r for r in records if r.event_type == "pipeline.reconciled" and str(r.pipeline_run_id) == run_id]
    assert len(events) == 1
    assert events[0].payload["reason"] == "jenkins_failure_reconciled"


def test_reconciler_repairs_lost_deployment_callback(monkeypatch):
    app_data = create_application("reconcile-cd-app")
    app_id = UUID(app_data["id"])
    dep_id = uuid4()

    dep = Deployment(
        id=dep_id,
        application_id=app_id,
        pipeline_run_id=None,
        runtime=Runtime.DOCKER,
        environment=Environment.STAGING,
        status=DeploymentStatus.DEPLOYING,
        artifact_digest="sha256:" + "c" * 64,
        fencing_token=1,
    )
    with main.platform._transaction() as s:
        unit = UnitOfWork(deployments=[(dep, None)])
        s.apply(unit)
        main.platform._acquire_lease(s, unit, dep, None, owner="test-dep")

    class MockCdOrchestrator(CdOrchestrator):
        def start_deployment(self, **kwargs):
            return "wf-1"

        def cancel(self, workflow_id: str, reason: str = ""):
            pass

        def get_status(self, workflow_id: str):
            # Workflow completed successfully
            return "completed"

    reconciler = Reconciler(
        platform=main.platform,
        ci_launcher=NullCiLauncher(),
        cd_orchestrator=MockCdOrchestrator(),
    )

    reconciled = reconciler.reconcile_deployments()
    assert len(reconciled) == 1
    assert reconciled[0]["deploymentId"] == str(dep_id)
    assert reconciled[0]["action"] == "reconciled_healthy"

    # Verify deployment is healthy and lease is released
    target = main.platform.lease_target(dep, None)
    with main.platform._transaction() as s:
        updated_dep = s.deployment(dep_id)
        assert updated_dep.status == DeploymentStatus.HEALTHY
        lease = s.active_deployment_lease(application_id=app_id, environment=Environment.STAGING.value, target=target)
        assert lease is None

    # Verify audit event in store
    with main.platform._transaction() as s:
        records = s.audit_records()
    events = [r for r in records if r.event_type == "deployment.reconciled" and r.deployment_id == dep_id]
    assert len(events) == 1
    assert events[0].payload["reason"] == "temporal_completed_callback_lost"


def test_reconciler_http_endpoint(monkeypatch):
    res = client.post(
        "/reconciler/reconcile",
        json={"timeoutSeconds": 7200},
    )
    assert res.status_code == 200, res.text
    data = res.json()
    assert "reconciledRuns" in data
    assert "reconciledDeployments" in data


# ==============================================================================
# 6. PostgreSQL Persistence for Stage Events and Retry Lineage
# ==============================================================================


@pytest.mark.skipif(not DATABASE_URL, reason="set NETCI_TEST_DATABASE_URL to a migrated PostgreSQL")
def test_postgres_pipeline_stages_and_retry_lineage(monkeypatch):
    import psycopg
    from app.store.postgres import PostgresDatabase

    monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
    db = PostgresDatabase(DATABASE_URL)

    # Wipe tables
    with psycopg.connect(DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute("TRUNCATE pipeline_stages, pipeline_runs, applications RESTART IDENTITY CASCADE")

    app_id = uuid4()
    run1_id = uuid4()
    run2_id = uuid4()

    app = Application(
        id=app_id,
        name="pg-lifecycle-app",
        repository_url="https://github.com/example/pg-lifecycle-app",
        pipeline_template="container-ci-cd-v1",
        runtime=Runtime.DOCKER,
        default_environment=Environment.DEV,
    )

    run1 = PipelineRun(
        id=run1_id,
        application_id=app_id,
        commit_sha="aaaa1111bbbb2222",
        branch="main",
        environment=Environment.DEV,
        status=PipelineStatus.FAILED,
    )

    # run2 retries run1
    run2 = PipelineRun(
        id=run2_id,
        application_id=app_id,
        commit_sha="aaaa1111bbbb2222",
        branch="main",
        environment=Environment.DEV,
        status=PipelineStatus.RUNNING,
        retry_of=run1_id,
    )

    with db.transaction() as s:
        unit = UnitOfWork(applications=[app], runs=[(run1, None), (run2, None)])
        s.apply(unit)

        # Record stage for run2
        stage = PipelineStage(
            id=uuid4(),
            pipeline_run_id=run2_id,
            stage_id="build",
            stage_name="Build Step",
            attempt=1,
            status="succeeded",
            duration_ms=4500,
            log_snippet="Step passed cleanly",
        )
        s.record_pipeline_stage(stage)

    # Read back and verify
    with db.transaction() as s:
        read_run1 = s.pipeline_run(run1_id)
        assert read_run1.retry_of is None

        read_run2 = s.pipeline_run(run2_id)
        assert read_run2.retry_of == run1_id

        stages = s.pipeline_stages(run2_id)
        assert len(stages) == 1
        assert stages[0].stage_id == "build"
        assert stages[0].status == "succeeded"
        assert stages[0].duration_ms == 4500
        assert stages[0].log_snippet == "Step passed cleanly"
