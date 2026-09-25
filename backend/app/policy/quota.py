"""Resource quota enforcement for pipelines, deployments, and production releases."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from ..domain.models import DeploymentStatus, PipelineStatus
from ..store.records import ResourceQuotaRecord
from ..store.session import PlatformSession
from .rules import PolicyViolation


class QuotaViolation(PolicyViolation):
    """Raised when an operation exceeds configured concurrency or rate quotas."""
    pass


@dataclass(frozen=True)
class PipelineScope:
    quota: ResourceQuotaRecord
    #: The applications whose runs count against this quota; None is all of them.
    application_ids: list[UUID] | None
    key: str


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


def _lock_key(text: str) -> int:
    """A stable signed 64-bit advisory-lock key for a scope name."""

    import hashlib

    return int.from_bytes(hashlib.sha256(text.encode()).digest()[:8], "big", signed=True)


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
    def pipeline_scope(
        cls,
        session: PlatformSession,
        *,
        application_id: UUID,
        team: str | None = None,
    ) -> "PipelineScope":
        """Which quota governs this application's builds, and which runs it counts.

        The count is over the quota's own scope. It used to be this application's runs
        whatever the scope, so a team quota of 10 allowed 10 per application. The built-in
        default (nothing configured) stays per application: counted platform-wide, it would
        cap every installation at five concurrent runs.
        """

        quota = cls.resolve_quota(session, application_id=application_id, team=team)
        if quota is DEFAULT_QUOTA or quota.scope == "application":
            return PipelineScope(quota, [application_id], f"application:{application_id}")
        if quota.scope == "team":
            apps = [a.id for a in session.applications() if a.owner_team == quota.scope_id]
            return PipelineScope(quota, apps, f"team:{quota.scope_id}")
        return PipelineScope(quota, None, f"{quota.scope}:{quota.scope_id}")

    @classmethod
    def check_queue_room(cls, session: PlatformSession, scope: "PipelineScope", *, freed: int = 0) -> None:
        """Refuse a new run only when its scope's queue is full (ADR-050).

        `freed` is how many waiting runs this very request supersedes: they leave the queue
        in the same transaction, so a pull request pushed again never finds its own queue
        full. A lock per scope, held until the run is written, stops two starts both
        seeing room for one.
        """

        session.advisory_xact_lock(_lock_key(f"netci-quota:{scope.key}"))
        waiting = session.count_waiting_pipeline_runs(scope.application_ids) - freed
        if waiting >= scope.quota.max_queued_pipelines:
            raise QuotaViolation(
                f"pipeline queue is full for scope {scope.key} "
                f"({waiting}/{scope.quota.max_queued_pipelines} waiting for admission)"
            )

    @staticmethod
    def admission_room(session: PlatformSession, scope: "PipelineScope") -> int:
        """How many more runs this scope may have holding CI capacity right now."""

        return scope.quota.max_concurrent_pipelines - session.count_admitted_pipeline_runs(scope.application_ids)

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
