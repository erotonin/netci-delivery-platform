from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import UUID, uuid4


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Runtime(str, Enum):
    DOCKER = "docker"
    KUBERNETES = "kubernetes"
    SYSTEMD = "systemd"


class Environment(str, Enum):
    DEV = "dev"
    STAGING = "staging"
    PROD = "prod"


class PipelineStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    ROLLED_BACK = "rolled_back"


class DeploymentStatus(str, Enum):
    PENDING_APPROVAL = "pending_approval"
    DEPLOYING = "deploying"
    HEALTHY = "healthy"
    FAILED = "failed"
    # A rollback is not instantaneous, and the window matters: "we are putting the old
    # version back" and "the old version is back" are different things to an operator
    # deciding whether to page someone.
    ROLLBACK_IN_PROGRESS = "rollback_in_progress"
    ROLLED_BACK = "rolled_back"
    # The state a platform that tells the truth has to be able to reach. A rollback that
    # failed leaves the environment in neither the new state nor the old one, and calling
    # that `failed` loses the fact that recovery was attempted and did not work.
    ROLLBACK_FAILED = "rollback_failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class Application:
    name: str
    repository_url: str
    pipeline_template: str
    runtime: Runtime
    default_environment: Environment = Environment.DEV
    stages: tuple[str, ...] = ()
    # The team accountable for this application. None means unowned: role checks still
    # apply, team checks do not. It is a plain string here because the domain has no
    # opinion about where team membership comes from -- a token file, an IdP group, or a
    # directory -- only that two names either match or do not.
    owner_team: str | None = None
    id: UUID = field(default_factory=uuid4)
    created_at: datetime = field(default_factory=utc_now)
    # Per-stage parameter values for the custom stages in `stages`.
    stage_parameters: dict[str, dict[str, str]] = field(default_factory=dict)


@dataclass(frozen=True)
class PipelineRun:
    application_id: UUID
    commit_sha: str
    environment: Environment
    branch: str = "main"
    parameters: dict[str, object] = field(default_factory=dict)
    correlation_id: str | None = None
    status: PipelineStatus = PipelineStatus.QUEUED
    # Subject of the verified principal that started the run. None only for runs created
    # before authentication existed; it is never taken from a request body.
    started_by: str | None = None
    id: UUID = field(default_factory=uuid4)
    jenkins_run_id: str | None = None
    workflow_id: str | None = None
    artifact_digest: str | None = None
    console_url: str | None = None
    retry_of: UUID | None = None
    config_revision_id: UUID | None = None
    # Decided when the run is queued and never changed (ADR-043). A build that is not
    # deployed ends `succeeded` with its digest and no deployment; a build that is not
    # published -- a fork's pull request -- is signed by nobody and has no digest, so
    # nothing of it can ever be deployed or promoted.
    deploy_after_build: bool = True
    publish_artifact: bool = True
    #: The tag this run was built from, when a rule registers it as a version on success.
    release_tag: str | None = None
    #: What started the run, for people: {event, ref, rule, reason, pullRequest, fromFork}.
    trigger: dict[str, object] = field(default_factory=dict)
    #: When the run was allowed to take CI capacity and was dispatched (ADR-050). A queued
    #: run with none is waiting for admission, not stuck.
    admitted_at: datetime | None = None
    #: `<application>:<ref>` for SCM runs of one branch or pull request; None for runs that
    #: supersede nothing and are superseded by nothing (tags, manual runs, retries).
    concurrency_group: str | None = None
    #: The newer run of the same group that cancelled this one.
    superseded_by: UUID | None = None
    #: When the run first left CI (terminal, or on to approval). With admitted_at, how long
    #: it held CI capacity; None for runs that have not left it, or predate the column.
    ci_finished_at: datetime | None = None
    version: int = 1
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)


class ConfigRevisionStatus(str, Enum):
    DRAFT = "draft"
    PENDING_APPROVAL = "pending_approval"
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    REJECTED = "rejected"


@dataclass(frozen=True)
class ModuleConfigRevision:
    module_id: str
    revision_number: int
    created_by: str
    pipeline_config: dict[str, Any] = field(default_factory=dict)
    deployment_config: list[dict[str, Any]] = field(default_factory=list)
    change_summary: str = ""
    status: ConfigRevisionStatus = ConfigRevisionStatus.ACTIVE
    approved_by: str | None = None
    approved_at: datetime | None = None
    rejection_reason: str | None = None
    id: UUID = field(default_factory=uuid4)
    created_at: datetime = field(default_factory=utc_now)


@dataclass(frozen=True)
class ServerHealthRecord:
    server_name: str
    status: str
    source: str
    freshness_seconds: int = 0
    details: dict[str, Any] = field(default_factory=dict)
    observed_at: datetime = field(default_factory=utc_now)


class ScmProviderType(str, Enum):
    GITHUB = "github"
    GITLAB = "gitlab"


class ScmCommitStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILURE = "failure"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class ScmIntegration:
    application_id: UUID
    provider: ScmProviderType
    repository_identity: str
    secret_token: str | None = None
    secret_token_hash: str | None = None
    credential_reference: str | None = None
    enabled: bool = True
    id: UUID = field(default_factory=uuid4)
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)


@dataclass(frozen=True)
class ScmWebhookDelivery:
    delivery_id: str
    provider: ScmProviderType
    event_type: str
    repository_identity: str
    status: str
    application_id: UUID | None = None
    commit_sha: str | None = None
    received_at: datetime = field(default_factory=utc_now)


class DeploymentStrategy(str, Enum):
    ROLLING = "rolling"
    CANARY = "canary"
    BLUE_GREEN = "blue_green"


@dataclass(frozen=True)
class Deployment:
    application_id: UUID
    runtime: Runtime
    environment: Environment
    artifact_digest: str
    pipeline_run_id: UUID | None = None
    previous_artifact_digest: str | None = None
    status: DeploymentStatus = DeploymentStatus.PENDING_APPROVAL
    id: UUID = field(default_factory=uuid4)
    approved_by: str | None = None
    # The lease generation this deployment was started under. A callback carrying a lower
    # token comes from a workflow that has since been superseded, and applying it would
    # let an older result overwrite a newer one.
    fencing_token: int | None = None
    config_revision_id: UUID | None = None
    strategy: str = "rolling"
    traffic_weight: int = 100
    active_color: str | None = None
    canary_step: int = 0
    #: When this deployment became healthy -- set once, by that transition. The start of
    #: a promotion soak (ADR-043); `updated_at` moves on every later transition.
    healthy_at: datetime | None = None
    version: int = 1
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)


class DeliveryEventType(str, Enum):
    """Source event kinds the DORA projection is allowed to read."""

    COMMIT = "commit"
    DEPLOYMENT = "deployment"
    RECOVERY = "recovery"


@dataclass(frozen=True)
class DeliveryEvent:
    """Durable delivery fact written in the same unit of work as the state change.

    The DORA projection reads only these rows, so a metric can always be traced
    back to the transition that produced it instead of to a UI fixture.
    """

    event_type: DeliveryEventType
    application_id: UUID
    occurred_at: datetime = field(default_factory=utc_now)
    commit_sha: str | None = None
    pipeline_run_id: UUID | None = None
    deployment_id: UUID | None = None
    environment: Environment | None = None
    successful: bool | None = None
    requires_intervention: bool = False
    id: UUID = field(default_factory=uuid4)


@dataclass(frozen=True)
class PipelineStage:
    pipeline_run_id: UUID
    stage_id: str
    stage_name: str
    status: str
    attempt: int = 1
    queued_at: datetime | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    duration_ms: int | None = None
    error_message: str | None = None
    log_snippet: str | None = None
    id: UUID = field(default_factory=uuid4)
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)


#: Which pipeline transitions are legal. This table is enforced -- `DeliveryPlatform`
#: consults it before every write -- so it cannot drift away from the code the way a
#: table nothing reads inevitably does.
#:
#: `QUEUED` deliberately does NOT reach `SUCCEEDED`. A queued run has not started, so
#: there is no build whose success could be reported; claiming one would be a green
#: light for work that never happened. A queued run that must be closed goes to `FAILED`
#: or `CANCELLED`. When an external engine reports success for a run netCI still thinks
#: is queued, the honest repair is to record the `RUNNING` transition that was lost and
#: then apply the result -- reconstructing what actually happened rather than skipping it.
PIPELINE_TRANSITIONS: dict[PipelineStatus, frozenset[PipelineStatus]] = {
    PipelineStatus.QUEUED: frozenset({PipelineStatus.RUNNING, PipelineStatus.FAILED, PipelineStatus.CANCELLED}),
    PipelineStatus.RUNNING: frozenset({PipelineStatus.WAITING_APPROVAL, PipelineStatus.SUCCEEDED, PipelineStatus.FAILED, PipelineStatus.CANCELLED}),
    PipelineStatus.WAITING_APPROVAL: frozenset({PipelineStatus.RUNNING, PipelineStatus.SUCCEEDED, PipelineStatus.FAILED, PipelineStatus.CANCELLED}),
    PipelineStatus.FAILED: frozenset({PipelineStatus.QUEUED, PipelineStatus.ROLLED_BACK}),
    PipelineStatus.SUCCEEDED: frozenset({PipelineStatus.ROLLED_BACK}),
    PipelineStatus.CANCELLED: frozenset(),
    PipelineStatus.ROLLED_BACK: frozenset(),
}


def can_transition_pipeline(current: PipelineStatus, target: PipelineStatus) -> bool:
    return target in PIPELINE_TRANSITIONS[current]


DEPLOYMENT_TRANSITIONS: dict[DeploymentStatus, frozenset[DeploymentStatus]] = {
    DeploymentStatus.PENDING_APPROVAL: frozenset(
        {DeploymentStatus.DEPLOYING, DeploymentStatus.FAILED, DeploymentStatus.CANCELLED}
    ),
    DeploymentStatus.DEPLOYING: frozenset(
        {DeploymentStatus.HEALTHY, DeploymentStatus.FAILED, DeploymentStatus.CANCELLED}
    ),
    DeploymentStatus.HEALTHY: frozenset({DeploymentStatus.ROLLBACK_IN_PROGRESS}),
    DeploymentStatus.FAILED: frozenset(
        {DeploymentStatus.DEPLOYING, DeploymentStatus.ROLLBACK_IN_PROGRESS}
    ),
    DeploymentStatus.ROLLBACK_IN_PROGRESS: frozenset(
        {DeploymentStatus.ROLLED_BACK, DeploymentStatus.ROLLBACK_FAILED}
    ),
    # A rollback that failed is not the end of the story: someone will try again.
    DeploymentStatus.ROLLBACK_FAILED: frozenset(
        {DeploymentStatus.ROLLBACK_IN_PROGRESS, DeploymentStatus.DEPLOYING}
    ),
    DeploymentStatus.ROLLED_BACK: frozenset(),
    DeploymentStatus.CANCELLED: frozenset(),
}


def can_transition_deployment(current: DeploymentStatus, target: DeploymentStatus) -> bool:
    return target in DEPLOYMENT_TRANSITIONS[current]


class NotificationStatus(str, Enum):
    PENDING = "pending"
    DELIVERED = "delivered"
    FAILED = "failed"
    DEAD_LETTER = "dead_letter"


@dataclass(frozen=True)
class NotificationRecord:
    id: UUID
    event_type: str
    aggregate_type: str
    aggregate_id: str
    payload: dict[str, Any]
    recipient: str
    status: NotificationStatus = NotificationStatus.PENDING
    attempt: int = 0
    max_attempts: int = 5
    last_attempt_at: datetime | None = None
    next_attempt_at: datetime = field(default_factory=utc_now)
    last_error: str | None = None
    created_at: datetime = field(default_factory=utc_now)
    delivered_at: datetime | None = None


class WaiverStatus(str, Enum):
    ACTIVE = "active"
    EXPIRED = "expired"
    REVOKED = "revoked"


@dataclass(frozen=True)
class SecurityWaiver:
    cve_id: str
    reason: str
    approved_by: str
    expires_at: datetime
    module_id: str | None = None
    status: WaiverStatus = WaiverStatus.ACTIVE
    id: UUID = field(default_factory=uuid4)
    created_at: datetime = field(default_factory=utc_now)

    @property
    def is_valid(self) -> bool:
        return self.status == WaiverStatus.ACTIVE and self.expires_at > utc_now()


@dataclass(frozen=True)
class ServerMaintenanceState:
    server_name: str
    in_maintenance: bool = True
    reason: str = ""
    updated_by: str = "operator"
    updated_at: datetime = field(default_factory=utc_now)


@dataclass(frozen=True)
class StageDefinition:
    """One entry of the stage catalog: a built-in the shared pipeline implements, or a
    custom stage that runs a repository script after a built-in one."""

    id: str
    name: str
    category: str
    kind: str  # builtin | custom
    position: int
    description: str = ""
    script: str | None = None
    after_stage: str | None = None
    required: bool = False
    enabled_by_default: bool = True
    created_by: str = "netci"
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    # Custom stages start `proposed` and run only once a second administrator approved.
    status: str = "active"
    approved_by: str | None = None
    # Declared parameters: ({"name": "LEVEL", "default": "strict", "description": ...}, ...)
    parameters: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class ServerTelemetry:
    """The latest observation an edge agent reported for one server."""

    server_name: str
    cpu_percent: float = 0.0
    mem_percent: float = 0.0
    disk_percent: float = 0.0
    observed_at: datetime = field(default_factory=utc_now)


@dataclass(frozen=True)
class AgentConnection:
    """An edge agent's websocket, held by one API replica (ADR-032)."""

    hostname: str
    agent_id: str
    replica_id: str
    token_jti: str
    connected_at: datetime = field(default_factory=utc_now)
    last_seen_at: datetime = field(default_factory=utc_now)


@dataclass(frozen=True)
class AgentCommand:
    """A diagnostic command for an agent, durable so any replica can dispatch or answer it."""

    id: UUID
    hostname: str
    command: str
    requested_by: str
    status: str = "pending"
    result: dict[str, Any] | None = None
    created_at: datetime = field(default_factory=utc_now)
    expires_at: datetime = field(default_factory=utc_now)
    claimed_by: str | None = None
    claimed_at: datetime | None = None
    completed_at: datetime | None = None


@dataclass(frozen=True)
class L7CanaryRule:
    header_name: str | None = None
    header_value: str | None = None
    cookie: str | None = None
    user_email_regex: str | None = None

