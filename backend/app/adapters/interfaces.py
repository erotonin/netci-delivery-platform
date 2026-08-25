from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from ..domain.models import Environment, Runtime


@dataclass(frozen=True)
class JenkinsRun:
    run_id: str
    status: str
    console_url: str | None = None


class JenkinsAdapter(Protocol):
    def create_or_update_job(self, application_id: UUID, template_id: str) -> str: ...
    def trigger_ci(self, job_name: str, commit_sha: str, correlation_id: str) -> JenkinsRun: ...
    def get_status(self, run_id: str) -> JenkinsRun: ...
    def get_logs(self, run_id: str) -> list[str]: ...
    def abort(self, run_id: str) -> None: ...


@dataclass(frozen=True)
class DeploymentRequest:
    application_id: UUID
    runtime: Runtime
    environment: Environment
    artifact_digest: str
    release_name: str
    parameters: dict[str, object]


@dataclass(frozen=True)
class DeploymentResult:
    deployment_id: str
    status: str
    endpoint: str | None = None
    revision: str | None = None


class RuntimeAdapter(Protocol):
    runtime: Runtime

    def validate(self, request: DeploymentRequest) -> None: ...
    def deploy(self, request: DeploymentRequest) -> DeploymentResult: ...
    def health_check(self, request: DeploymentRequest) -> bool: ...
    def rollback(self, request: DeploymentRequest, target_artifact_digest: str) -> DeploymentResult: ...


class ArtifactStore(Protocol):
    def put(self, key: str, content: bytes, content_type: str) -> str: ...
    def get(self, key: str) -> bytes: ...


class ImageBuilder(Protocol):
    def build(self, source_dir: str, image_ref: str) -> str: ...
    def push(self, image_ref: str) -> str: ...


class PolicyEngine(Protocol):
    def check_artifact(self, artifact_digest: str, environment: Environment) -> None: ...
    def can_deploy(self, runtime: Runtime, environment: Environment, actor: str) -> bool: ...
