from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
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
    ROLLED_BACK = "rolled_back"


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
    version: int = 1
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)


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


PIPELINE_TRANSITIONS: dict[PipelineStatus, frozenset[PipelineStatus]] = {
    PipelineStatus.QUEUED: frozenset({PipelineStatus.RUNNING, PipelineStatus.CANCELLED}),
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
    DeploymentStatus.PENDING_APPROVAL: frozenset({DeploymentStatus.DEPLOYING, DeploymentStatus.FAILED}),
    DeploymentStatus.DEPLOYING: frozenset({DeploymentStatus.HEALTHY, DeploymentStatus.FAILED}),
    DeploymentStatus.HEALTHY: frozenset({DeploymentStatus.ROLLED_BACK}),
    DeploymentStatus.FAILED: frozenset({DeploymentStatus.DEPLOYING, DeploymentStatus.ROLLED_BACK}),
    DeploymentStatus.ROLLED_BACK: frozenset(),
}


def can_transition_deployment(current: DeploymentStatus, target: DeploymentStatus) -> bool:
    return target in DEPLOYMENT_TRANSITIONS[current]
