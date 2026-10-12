"""Tests for Phase 11 Resource Quotas & Concurrency Limits."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from dataclasses import replace
import pytest
from fastapi.testclient import TestClient

from app.domain.models import Application, Deployment, DeploymentStatus, Environment, PipelineRun, PipelineStatus, Runtime
from app.main import app
from app.persistence import UnitOfWork
from app.policy.quota import QuotaEnforcer, QuotaViolation
from app.store.memory import InMemoryDatabase
from app.store.records import ResourceQuotaRecord


def test_quota_resolution_hierarchy():
    db = InMemoryDatabase()
    app_id = uuid4()

    with db.transaction() as session:
        # Default global quota when nothing configured
        q_default = QuotaEnforcer.resolve_quota(session, application_id=app_id, team="payments")
        assert q_default.scope == "global"
        assert q_default.max_concurrent_pipelines == 5

        # Configure team quota
        session.set_resource_quota(
            ResourceQuotaRecord(
                id=uuid4(),
                scope="team",
                scope_id="payments",
                max_concurrent_pipelines=10,
                max_concurrent_deployments=4,
            )
        )
        q_team = QuotaEnforcer.resolve_quota(session, application_id=app_id, team="payments")
        assert q_team.scope == "team"
        assert q_team.max_concurrent_pipelines == 10

        # Configure application quota which overrides team
        session.set_resource_quota(
            ResourceQuotaRecord(
                id=uuid4(),
                scope="application",
                scope_id=str(app_id),
                max_concurrent_pipelines=3,
                max_concurrent_deployments=1,
            )
        )
        q_app = QuotaEnforcer.resolve_quota(session, application_id=app_id, team="payments")
        assert q_app.scope == "application"
        assert q_app.max_concurrent_pipelines == 3


def test_pipeline_concurrency_quota_enforcement():
    db = InMemoryDatabase()
    app_id = uuid4()

    with db.transaction() as session:
        session.set_resource_quota(
            ResourceQuotaRecord(
                id=uuid4(),
                scope="application",
                scope_id=str(app_id),
                max_concurrent_pipelines=2,
            )
        )

        scope = QuotaEnforcer.pipeline_scope(session, application_id=app_id)
        assert QuotaEnforcer.admission_room(session, scope) == 2

        def run(status, *, admitted):
            now = datetime.now(timezone.utc)
            return PipelineRun(
                id=uuid4(), application_id=app_id, status=status, commit_sha="abcdef123456",
                branch="main", environment=Environment.DEV, parameters={}, correlation_id="c",
                started_by="dev1", admitted_at=now if admitted else None, created_at=now, updated_at=now,
            )

        # Holding CI capacity: admitted and not finished (ADR-050).
        session.apply(UnitOfWork(runs=[(run(PipelineStatus.RUNNING, admitted=True), None)]))
        session.apply(UnitOfWork(runs=[(run(PipelineStatus.QUEUED, admitted=True), None)]))
        assert QuotaEnforcer.admission_room(session, scope) == 0
        # Waiting and waiting-for-approval runs hold none; finished ones hold none.
        session.apply(UnitOfWork(runs=[(run(PipelineStatus.QUEUED, admitted=False), None)]))
        session.apply(UnitOfWork(runs=[(run(PipelineStatus.WAITING_APPROVAL, admitted=True), None)]))
        session.apply(UnitOfWork(runs=[(run(PipelineStatus.SUCCEEDED, admitted=True), None)]))
        assert QuotaEnforcer.admission_room(session, scope) == 0
        assert session.count_waiting_pipeline_runs([app_id]) == 1

        # The queue is what refuses, and only when full.
        QuotaEnforcer.check_queue_room(session, scope)
        tight = QuotaEnforcer.pipeline_scope(session, application_id=app_id)
        tight = type(tight)(replace(tight.quota, max_queued_pipelines=1), tight.application_ids, tight.key)
        with pytest.raises(QuotaViolation, match="queue is full"):
            QuotaEnforcer.check_queue_room(session, tight)
        # ...unless this very request supersedes the waiting run.
        QuotaEnforcer.check_queue_room(session, tight, freed=1)


def test_resource_quota_api_flow():
    client = TestClient(app)

    # 1. Get default quota
    get_resp = client.get("/quotas/team/analytics")
    assert get_resp.status_code == 200
    assert get_resp.json()["maxConcurrentPipelines"] == 5

    # 2. Update quota
    put_resp = client.put(
        "/quotas/team/analytics",
        json={
            "maxConcurrentPipelines": 12,
            "maxConcurrentDeployments": 6,
            "maxProductionRequestsPerDay": 50,
        },
    )
    assert put_resp.status_code == 200
    updated = put_resp.json()
    assert updated["maxConcurrentPipelines"] == 12
    assert updated["maxConcurrentDeployments"] == 6

    # 3. Verify get returns updated quota
    get_updated = client.get("/quotas/team/analytics")
    assert get_updated.status_code == 200
    assert get_updated.json()["maxConcurrentPipelines"] == 12
