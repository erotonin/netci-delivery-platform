"""Resource quota enforcement for pipelines, deployments, and production releases."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID, uuid4

from ..domain.models import DeploymentStatus, PipelineStatus
from ..store.records import ResourceQuotaRecord
from ..store.session import PlatformSession
from .rules import PolicyViolation


class QuotaViolation(PolicyViolation):
    """Raised when an operation exceeds configured concurrency or rate quotas."""
    pass


DEFAULT_QUOTA = ResourceQuotaRecord(
    id=UUID("00000000-0000-0000-0000-000000000000"),
    scope="global",
    scope_id="global",
    max_concurrent_pipelines=5,
    max_concurrent_deployments=2,
    max_production_requests_per_day=20,
)

ACTIVE_PIPELINE_STATUSES = {
    PipelineStatus.QUEUED,
    PipelineStatus.RUNNING,
    PipelineStatus.WAITING_APPROVAL,
}

ACTIVE_DEPLOYMENT_STATUSES = {
    DeploymentStatus.PENDING_APPROVAL,
    DeploymentStatus.DEPLOYING,
    DeploymentStatus.ROLLBACK_IN_PROGRESS,
}


class QuotaEnforcer:
    """Evaluates and enforces concurrency and rate limits hierarchically."""

    @staticmethod
    def resolve_quota(
        session: PlatformSession,
        *,
        application_id: UUID | None = None,
        team: str | None = None,
    ) -> ResourceQuotaRecord:
        # 1. Application-scoped quota
        if application_id:
            a_quota = session.get_resource_quota("application", str(application_id))
            if a_quota:
                return a_quota

        # 2. Team-scoped quota
        if team:
            t_quota = session.get_resource_quota("team", team)
            if t_quota:
                return t_quota

        # 3. Global quota
        g_quota = session.get_resource_quota("global", "global")
        if g_quota:
            return g_quota

        return DEFAULT_QUOTA

    @classmethod
    def check_pipeline_quota(
        cls,
        session: PlatformSession,
        *,
        application_id: UUID,
        team: str | None = None,
    ) -> None:
        quota = cls.resolve_quota(session, application_id=application_id, team=team)
        runs = session.pipeline_runs(application_id)
        active_count = sum(1 for r in runs if r.status in ACTIVE_PIPELINE_STATUSES)
        if active_count >= quota.max_concurrent_pipelines:
            raise QuotaViolation(
                f"Concurrent pipeline limit reached for scope {quota.scope}:{quota.scope_id} "
                f"({active_count}/{quota.max_concurrent_pipelines} active)"
            )

    @classmethod
    def check_deployment_quota(
        cls,
        session: PlatformSession,
        *,
        application_id: UUID,
        team: str | None = None,
    ) -> None:
        quota = cls.resolve_quota(session, application_id=application_id, team=team)
        deployments = session.deployments(application_id)
        active_count = sum(1 for d in deployments if d.status in ACTIVE_DEPLOYMENT_STATUSES)
        if active_count >= quota.max_concurrent_deployments:
            raise QuotaViolation(
                f"Concurrent deployment limit reached for scope {quota.scope}:{quota.scope_id} "
                f"({active_count}/{quota.max_concurrent_deployments} active)"
            )
