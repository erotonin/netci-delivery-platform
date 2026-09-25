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
    created_at: datetime | None = None


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
class ChangeFreezeRecord:
    """A window in which deployments are refused (ADR-047)."""

    id: UUID
    name: str
    starts_at: datetime
    ends_at: datetime
    environments: tuple[str, ...]
    reason: str
    created_by: str
    system_id: str | None = None
    module_id: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    cancelled_at: datetime | None = None
    cancelled_by: str | None = None


@dataclass(frozen=True)
class ArtifactSbomRecord:
    """The SBOM of one immutable artifact, as CI produced it (ADR-045)."""

    artifact_digest: str
    application_id: UUID
    pipeline_run_id: UUID
    format: str
    document: dict[str, Any]
    component_count: int
    recorded_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass(frozen=True)
class ArtifactFindingRecord:
    artifact_digest: str
    source: str  # ci | rescan
    vulnerability_id: str
    severity: str
    package: str = ""
    installed_version: str = ""
    fixed_version: str = ""
    first_seen_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_seen_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def key(self) -> tuple[str, str, str, str, str]:
        return (self.artifact_digest, self.source, self.vulnerability_id, self.package, self.installed_version)


@dataclass(frozen=True)
class ArtifactRescanRecord:
    artifact_digest: str
    scanned_at: datetime
    status: str  # scanned | failed
    scanner: str
    detail: str = ""


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


@dataclass(frozen=True)
class CatalogServiceRecord:
    id: str
    name: str
    description: str
    owning_team: str
    tier: str = "tier-2"
    lifecycle: str = "active"
    repo_url: str = ""
    docs_url: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass(frozen=True)
class ServiceDependencyRecord:
    id: UUID
    source_service_id: str
    target_service_id: str
    dependency_type: str = "sync"
    description: str = ""
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass(frozen=True)
class CatalogTemplateRecord:
    id: str
    version: str
    name: str
    description: str
    category: str = "backend"
    parameters_schema: dict[str, Any] = field(default_factory=dict)
    pipeline_definition: dict[str, Any] = field(default_factory=dict)
    is_deprecated: bool = False
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


#: Preview states that may still have a namespace in the cluster, so teardown -- by a
#: person, a closed pull request or the TTL -- applies to them. `failed` belongs here: a
#: deploy that failed after creating its namespace left it there, and nothing else would
#: ever remove it. Tearing down one that was never created is a no-op in the playbook.
PREVIEW_TEARDOWN_STATES = ("active", "deploying", "failed")


@dataclass(frozen=True)
class PreviewEnvironmentRecord:
    """A pull request's own deployment (ADR-049).

    `url` is nullable: it is written only once the worker reports what the cluster
    actually served, never composed here. `pipeline_run_id` and `artifact_digest` name
    the build the row was last (re)deployed from; `release_name` is the Helm release the
    worker manages, deterministic from the module id and pull request number so a second
    push to the same PR finds and redeploys the same row instead of creating another.
    """

    id: str
    application_id: UUID
    pull_request_id: str
    commit_sha: str
    namespace: str
    url: str | None
    status: str = "pending"
    ttl_seconds: int = 86400
    expires_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    created_by: str = "system"
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    destroyed_at: datetime | None = None
    pipeline_run_id: UUID | None = None
    artifact_digest: str | None = None
    release_name: str | None = None
    detail: str = ""

    def is_active(self, now: datetime) -> bool:
        return self.status == "active" and self.expires_at > now and self.destroyed_at is None

    def is_expired(self, now: datetime) -> bool:
        return self.status in PREVIEW_TEARDOWN_STATES and self.expires_at <= now


@dataclass(frozen=True)
class ResourceRequestRecord:
    id: UUID
    application_id: UUID
    team_id: str
    environment: str = "preview"
    resource_type: str = "postgres_database"
    spec: dict[str, Any] = field(default_factory=dict)
    status: str = "pending_approval"
    status_reason: str = ""
    provider: str = "unconfigured"
    outputs: dict[str, Any] = field(default_factory=dict)
    requested_by: str = "developer"
    approved_by: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

