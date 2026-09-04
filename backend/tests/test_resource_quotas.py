"""Tests for Phase 11 Resource Quotas & Concurrency Limits."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

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

        # 1 active run
        run1 = PipelineRun(
            id=uuid4(),
            application_id=app_id,
            status=PipelineStatus.RUNNING,
            commit_sha="abcdef123456",
            branch="main",
            environment=Environment.DEV,
            parameters={},
            correlation_id="c-1",
            jenkins_run_id=1,
            workflow_id=None,
            artifact_digest=None,
            started_by="dev1",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        session.apply(UnitOfWork(runs=[(run1, None)]))
        # Should pass
        QuotaEnforcer.check_pipeline_quota(session, application_id=app_id)

        # 2nd active run
        run2 = PipelineRun(
            id=uuid4(),
            application_id=app_id,
            status=PipelineStatus.QUEUED,
            commit_sha="abcdef123457",
            branch="main",
            environment=Environment.DEV,
            parameters={},
            correlation_id="c-2",
            jenkins_run_id=2,
            workflow_id=None,
            artifact_digest=None,
            started_by="dev2",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        session.apply(UnitOfWork(runs=[(run2, None)]))
        # Reached limit -> raises QuotaViolation
        with pytest.raises(QuotaViolation, match="Concurrent pipeline limit reached"):
            QuotaEnforcer.check_pipeline_quota(session, application_id=app_id)


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
