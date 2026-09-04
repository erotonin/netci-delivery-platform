"""Row shapes the Portal hierarchy is read and written as.

These are deliberately plain: the Portal service owns the response shapes, and the
store owns durability. Keeping the boundary at a dataclass stops SQL column names
from leaking into the API layer and stops response formatting from leaking into SQL.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import UUID


@dataclass(frozen=True)
class SystemRow:
    id: str
    unit: str
    description: str
    owner: str
    status: str


@dataclass(frozen=True)
class ModuleRow:
    id: str
    system_id: str
    name: str
    module_type: str
    description: str
    runtime: str
    application_id: UUID | None = None
    deployment_config: list[dict[str, Any]] = field(default_factory=list)
    pipeline_config: dict[str, Any] = field(default_factory=dict)
    active_config_revision_id: UUID | None = None
    config_version: int = 1


@dataclass(frozen=True)
class VersionRow:
    module_id: str
    version: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RequestModuleRow:
    module_id: str
    version: str
    deployment_order: int = 1
    dependencies: tuple[str, ...] = ()
    status: str = "pending"
    deployment_id: UUID | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    error_message: str | None = None


@dataclass(frozen=True)
class RequestRow:
    id: str
    modules: tuple[RequestModuleRow, ...]
    requested_by: str
    scheduled_for: datetime
    rollback_strategy: str
    run_automation_tests: bool
    status: str
    deployment_id: UUID | None = None
    comment: str | None = None
    idempotency_key: str | None = None
    request_hash: str | None = None
    release_plan: dict[str, Any] | None = None
    strategy: str = "rolling"
    strategy_config: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DeploymentLease:
    """Exclusive claim on one deployment target, held for a bounded time.

    The lease is what stops two workflows writing to the same hosts. The fencing token is
    what stops the loser writing *afterwards*: it rises every time the target is claimed,
    every callback carries the one its deployment was started under, and a lower one is
    refused. Without it, a workflow that paused past its expiry could come back and
    overwrite the result of the workflow that replaced it.
    """

    id: UUID
    application_id: UUID
    environment: str
    target: str
    deployment_id: UUID
    owner: str
    fencing_token: int
    acquired_at: datetime
    heartbeat_at: datetime
    expires_at: datetime
    released_at: datetime | None = None
    release_reason: str | None = None

    def is_expired(self, now: datetime) -> bool:
        return self.released_at is None and self.expires_at <= now


@dataclass(frozen=True)
class PolicyDecisionRecord:
    id: UUID
    scope: str
    target_type: str
    target_id: str
    allowed: bool
    reason: str
    risk_score: int = 0
    checks: dict[str, Any] = field(default_factory=dict)
    rules_evaluated: list[str] = field(default_factory=list)
    evaluator: str = "builtin"
    evaluated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SecurityExceptionRecord:
    id: UUID
    cve: str
    artifact_digest: str
    owner: str
    reason: str
    approved_by: str
    status: str
    created_at: datetime
    expires_at: datetime
    revoked_at: datetime | None = None
    revoked_by: str | None = None

    def is_active(self, now: datetime) -> bool:
        return self.status == "active" and self.expires_at > now and self.revoked_at is None


@dataclass(frozen=True)
class BreakGlassRecord:
    id: UUID
    target_type: str
    target_id: str
    requested_by: str
    reason: str
    incident_ticket: str
    status: str
    created_at: datetime
    approved_by: str | None = None
    approved_at: datetime | None = None
    expires_at: datetime | None = None

    def is_active(self, now: datetime) -> bool:
        return self.status == "active" and self.expires_at is not None and self.expires_at > now


@dataclass(frozen=True)
class ResourceQuotaRecord:
    id: UUID
    scope: str
    scope_id: str
    max_concurrent_pipelines: int = 5
    max_concurrent_deployments: int = 2
    max_production_requests_per_day: int = 20
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
