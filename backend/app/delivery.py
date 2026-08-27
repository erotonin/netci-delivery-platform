"""Application-layer module for the local netCI delivery reference implementation.

The in-memory implementation deliberately performs no Jenkins, Temporal, database,
or runtime I/O.  HTTP routes and tests use this module's interface; production
adapters can replace its storage/execution details without moving delivery rules
back into the transport layer.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from uuid import UUID

from .persistence import PostgresDeliveryStore
from .domain.models import (
    Application,
    Deployment,
    DeploymentStatus,
    Environment,
    PipelineRun,
    PipelineStatus,
    Runtime,
)


IMMUTABLE_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")

STAGE_CATALOG: tuple[dict[str, object], ...] = (
    {"id": "checkout", "name": "Checkout source", "category": "source", "enabledByDefault": True},
    {"id": "unit-test", "name": "Unit tests", "category": "test", "enabledByDefault": True},
    {"id": "build", "name": "Build artifact/image", "category": "build", "enabledByDefault": True},
    {"id": "sbom", "name": "Generate SBOM", "category": "security", "enabledByDefault": True},
    {"id": "vulnerability-scan", "name": "Vulnerability scan", "category": "security", "enabledByDefault": True},
    {"id": "sign", "name": "Sign artifact", "category": "publish", "enabledByDefault": True},
    {"id": "publish", "name": "Publish artifact", "category": "publish", "enabledByDefault": True},
    {"id": "deploy", "name": "Deploy through netCI", "category": "deploy", "enabledByDefault": True},
    {"id": "health-check", "name": "Health check", "category": "verify", "enabledByDefault": True},
)


@dataclass(frozen=True)
class TemplateDefinition:
    runtime: Runtime
    stages: tuple[str, ...]


TEMPLATES: dict[str, TemplateDefinition] = {
    "container-ci-cd-v1": TemplateDefinition(
        Runtime.DOCKER,
        ("checkout", "unit-test", "build", "sbom", "vulnerability-scan", "sign", "publish", "deploy", "health-check"),
    ),
    "kubernetes-ci-cd-v1": TemplateDefinition(
        Runtime.KUBERNETES,
        ("checkout", "unit-test", "build", "sbom", "vulnerability-scan", "sign", "publish", "deploy", "health-check"),
    ),
    "systemd-ansible-ci-cd-v1": TemplateDefinition(
        Runtime.SYSTEMD,
        ("checkout", "unit-test", "build", "publish", "deploy", "health-check"),
    ),
}


class DeliveryError(Exception):
    def __init__(self, code: str, message: str, status_code: int) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


@dataclass(frozen=True)
class CiResult:
    pipeline_run: PipelineRun
    deployment: Deployment | None = None


@dataclass(frozen=True)
class IdempotencyRecord:
    request_hash: str
    result: Application | PipelineRun


class DeliveryPlatform:
    """Own local delivery state behind one application-layer interface."""

    def __init__(self) -> None:
        self._applications: dict[UUID, Application] = {}
        self._pipeline_runs: dict[UUID, PipelineRun] = {}
        self._deployments: dict[UUID, Deployment] = {}
        self._pipeline_logs: dict[UUID, list[str]] = {}
        self._idempotency_records: dict[tuple[object, ...], IdempotencyRecord] = {}
        self._store = PostgresDeliveryStore.from_env()
        self._load_persistent_state()

    def _load_persistent_state(self) -> None:
        if self._store is None:
            return
        data = self._store.load()
        if not data:
            return
        self._applications = {item.id: item for item in data['applications']}
        self._pipeline_runs = {item.id: item for item in data['runs']}
        self._deployments = {item.id: item for item in data['deployments']}
        self._pipeline_logs = {item.id: [] for item in data['runs']}

    def _persist(self, method: str, *args: object) -> None:
        if self._store is None:
            return
        try:
            getattr(self._store, method)(*args)
        except Exception as exc:
            raise DeliveryError('PERSISTENCE_UNAVAILABLE', f'cannot persist delivery state: {exc}', 503) from exc

    def reset(self) -> None:
        """Clear the local adapter; intended for tests and explicit local reset."""

        self._applications.clear()
        self._pipeline_runs.clear()
        self._deployments.clear()
        self._pipeline_logs.clear()
        self._idempotency_records.clear()
        self._load_persistent_state()

    def stage_catalog(self) -> dict[str, list[dict[str, object]]]:
        templates = [
            {
                "id": template_id,
                "name": template_id,
                "runtime": template.runtime.value,
                "stageIds": list(template.stages),
            }
            for template_id, template in TEMPLATES.items()
        ]
        return {"stages": [dict(stage) for stage in STAGE_CATALOG], "templates": templates}

    def create_application(
        self,
        *,
        name: str,
        repository_url: str,
        pipeline_template: str,
        runtime: Runtime,
        default_environment: Environment,
        stages: list[str],
        idempotency_key: str | None,
    ) -> Application:
        request_payload = {
            "name": name,
            "repositoryUrl": repository_url,
            "pipelineTemplate": pipeline_template,
            "runtime": runtime.value,
            "defaultEnvironment": default_environment.value,
            "stages": stages,
        }
        scope = ("application.create", idempotency_key)
        replay = self._idempotent_replay(scope, idempotency_key, request_payload)
        if replay is not None:
            if not isinstance(replay, Application):
                raise AssertionError("application idempotency scope returned another result type")
            return replay

        template = TEMPLATES.get(pipeline_template)
        if template is None:
            raise DeliveryError("TEMPLATE_NOT_FOUND", "pipeline template does not exist", 422)
        if template.runtime != runtime:
            raise DeliveryError(
                "RUNTIME_TEMPLATE_MISMATCH",
                "runtime does not match pipeline template",
                422,
            )
        selected_stages = self._validate_stages(template, stages)
        if any(item.name == name for item in self._applications.values()):
            raise DeliveryError("APPLICATION_EXISTS", "application name already exists", 409)

        application = Application(
            name=name,
            repository_url=repository_url,
            pipeline_template=pipeline_template,
            runtime=runtime,
            default_environment=default_environment,
            stages=selected_stages,
        )
        self._applications[application.id] = application
        self._persist('save_application', application)
        self._persist('save_audit_event', 'application.created', application, None, None)
        self._remember(scope, idempotency_key, request_payload, application)
        return application

    def list_applications(self) -> tuple[Application, ...]:
        return tuple(self._applications.values())

    def start_pipeline(
        self,
        application_id: UUID,
        *,
        commit_sha: str,
        branch: str,
        environment: Environment,
        parameters: dict[str, object],
        correlation_id: str,
        idempotency_key: str | None,
    ) -> PipelineRun:
        if application_id not in self._applications:
            raise DeliveryError("APPLICATION_NOT_FOUND", "application not found", 404)
        request_payload = {
            "commitSha": commit_sha,
            "branch": branch,
            "environment": environment.value,
            "parameters": parameters,
        }
        scope = ("pipeline.start", application_id, idempotency_key)
        replay = self._idempotent_replay(scope, idempotency_key, request_payload)
        if replay is not None:
            if not isinstance(replay, PipelineRun):
                raise AssertionError("pipeline idempotency scope returned another result type")
            return replay

        run = PipelineRun(
            application_id=application_id,
            commit_sha=commit_sha,
            branch=branch,
            environment=environment,
            parameters=dict(parameters),
            correlation_id=correlation_id,
        )
        self._pipeline_runs[run.id] = run
        self._persist('save_pipeline_run', run)
        self._persist('save_audit_event', 'pipeline.started', run, None, correlation_id)
        self._pipeline_logs[run.id] = [
            f"queued correlationId={correlation_id}",
            f"commit={run.commit_sha}",
        ]
        self._remember(scope, idempotency_key, request_payload, run)
        return run

    def list_pipeline_runs(self, application_id: UUID | None = None) -> tuple[PipelineRun, ...]:
        runs = tuple(self._pipeline_runs.values())
        if application_id is None:
            return runs
        return tuple(run for run in runs if run.application_id == application_id)

    def list_deployments(self, application_id: UUID | None = None) -> tuple[Deployment, ...]:
        deployments = tuple(self._deployments.values())
        if application_id is None:
            return deployments
        return tuple(item for item in deployments if item.application_id == application_id)

    def get_pipeline(self, pipeline_run_id: UUID) -> PipelineRun:
        run = self._pipeline_runs.get(pipeline_run_id)
        if run is None:
            raise DeliveryError("PIPELINE_NOT_FOUND", "pipeline run not found", 404)
        return run

    def get_pipeline_logs(self, pipeline_run_id: UUID) -> tuple[PipelineRun, tuple[str, ...]]:
        run = self.get_pipeline(pipeline_run_id)
        return run, tuple(self._pipeline_logs.get(pipeline_run_id, ()))

    def get_deployment(self, deployment_id: UUID) -> Deployment:
        deployment = self._deployments.get(deployment_id)
        if deployment is None:
            raise DeliveryError("DEPLOYMENT_NOT_FOUND", "deployment not found", 404)
        return deployment

    @staticmethod
    def _validate_stages(template: TemplateDefinition, stages: list[str]) -> tuple[str, ...]:
        if not stages:
            return template.stages
        positions = {stage_id: index for index, stage_id in enumerate(template.stages)}
        unknown = [stage_id for stage_id in stages if stage_id not in positions]
        duplicated = len(stages) != len(set(stages))
        declared_positions = [positions[stage_id] for stage_id in stages if stage_id in positions]
        out_of_order = declared_positions != sorted(declared_positions)
        if unknown or duplicated or out_of_order:
            raise DeliveryError(
                "INVALID_STAGES",
                "stages must be unique members of the selected template in catalog order",
                422,
            )
        return tuple(stages)

    def _idempotent_replay(
        self,
        scope: tuple[object, ...],
        idempotency_key: str | None,
        request_payload: object,
    ) -> Application | PipelineRun | None:
        if idempotency_key is None:
            return None
        record = self._idempotency_records.get(scope)
        if record is None:
            return None
        if record.request_hash != self._payload_hash(request_payload):
            raise DeliveryError(
                "IDEMPOTENCY_KEY_REUSED",
                "same key was used with a different request",
                409,
            )
        return record.result

    def _remember(
        self,
        scope: tuple[object, ...],
        idempotency_key: str | None,
        request_payload: object,
        result: Application | PipelineRun,
    ) -> None:
        if idempotency_key is not None:
            self._idempotency_records[scope] = IdempotencyRecord(
                self._payload_hash(request_payload),
                result,
            )

    @staticmethod
    def _payload_hash(payload: object) -> str:
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(canonical.encode()).hexdigest()

    def record_ci_result(
        self,
        pipeline_run_id: UUID,
        result_status: str,
        artifact_digest: str | None,
        log_lines: list[str],
    ) -> CiResult:
        run = self._pipeline_runs.get(pipeline_run_id)
        if run is None:
            raise DeliveryError("PIPELINE_NOT_FOUND", "pipeline run not found", 404)

        if result_status == PipelineStatus.RUNNING.value:
            if run.status != PipelineStatus.QUEUED:
                raise DeliveryError("INVALID_PIPELINE_STATE", "pipeline run is not queued", 409)
            updated = replace(run, status=PipelineStatus.RUNNING, updated_at=datetime.now(timezone.utc))
            self._pipeline_runs[updated.id] = updated
            self._persist('save_pipeline_run', updated)
            self._pipeline_logs[updated.id].extend(log_lines)
            return CiResult(updated)

        if result_status not in {PipelineStatus.SUCCEEDED.value, PipelineStatus.FAILED.value}:
            raise DeliveryError("INVALID_CI_RESULT", "CI result status is not supported", 422)
        if run.status != PipelineStatus.RUNNING:
            raise DeliveryError("INVALID_PIPELINE_STATE", "pipeline run is not running", 409)

        self._pipeline_logs[run.id].extend(log_lines)
        if result_status == PipelineStatus.FAILED.value:
            updated = replace(run, status=PipelineStatus.FAILED, updated_at=datetime.now(timezone.utc))
            self._pipeline_runs[updated.id] = updated
            self._persist('save_pipeline_run', updated)
            self._persist('save_audit_event', 'pipeline.failed', updated, None, updated.correlation_id)
            return CiResult(updated)

        if not artifact_digest or not IMMUTABLE_DIGEST.fullmatch(artifact_digest):
            raise DeliveryError(
                "IMMUTABLE_ARTIFACT_REQUIRED",
                "successful CI requires a sha256 artifact digest",
                422,
            )

        application = self._applications[run.application_id]
        requires_approval = run.environment.value == "prod"
        deployment = Deployment(
            application_id=run.application_id,
            pipeline_run_id=run.id,
            runtime=application.runtime,
            environment=run.environment,
            artifact_digest=artifact_digest,
            status=(
                DeploymentStatus.PENDING_APPROVAL
                if requires_approval
                else DeploymentStatus.DEPLOYING
            ),
        )
        updated = replace(
            run,
            status=(PipelineStatus.WAITING_APPROVAL if requires_approval else PipelineStatus.RUNNING),
            artifact_digest=artifact_digest,
            updated_at=datetime.now(timezone.utc),
        )
        self._deployments[deployment.id] = deployment
        self._persist('save_deployment', deployment)
        self._pipeline_runs[updated.id] = updated
        self._persist('save_pipeline_run', updated)
        self._persist('save_audit_event', 'pipeline.succeeded', updated, None, updated.correlation_id)
        return CiResult(updated, deployment)

    def approve_deployment(self, deployment_id: UUID, actor: str) -> Deployment:
        deployment = self._deployments.get(deployment_id)
        if deployment is None:
            raise DeliveryError("DEPLOYMENT_NOT_FOUND", "deployment not found", 404)
        if deployment.status != DeploymentStatus.PENDING_APPROVAL:
            raise DeliveryError(
                "INVALID_DEPLOYMENT_STATE",
                "deployment is not waiting for approval",
                409,
            )

        now = datetime.now(timezone.utc)
        updated = replace(
            deployment,
            status=DeploymentStatus.DEPLOYING,
            approved_by=actor,
            updated_at=now,
        )
        self._deployments[updated.id] = updated
        self._persist('save_deployment', updated)
        self._persist('save_audit_event', 'deployment.approved', updated, actor, None)
        if deployment.pipeline_run_id is not None:
            run = self._pipeline_runs[deployment.pipeline_run_id]
            self._pipeline_runs[run.id] = replace(
                run,
                status=PipelineStatus.RUNNING,
                updated_at=now,
            )
            self._persist('save_pipeline_run', self._pipeline_runs[run.id])
        return updated

    def record_deployment_result(
        self,
        deployment_id: UUID,
        result_status: str,
        message: str | None,
    ) -> Deployment:
        deployment = self._deployments.get(deployment_id)
        if deployment is None:
            raise DeliveryError("DEPLOYMENT_NOT_FOUND", "deployment not found", 404)
        if deployment.status != DeploymentStatus.DEPLOYING:
            raise DeliveryError("INVALID_DEPLOYMENT_STATE", "deployment is not deploying", 409)
        if result_status not in {DeploymentStatus.HEALTHY.value, DeploymentStatus.FAILED.value}:
            raise DeliveryError("INVALID_DEPLOYMENT_RESULT", "deployment result is not supported", 422)

        now = datetime.now(timezone.utc)
        target_status = DeploymentStatus(result_status)
        updated = replace(deployment, status=target_status, updated_at=now)
        self._deployments[updated.id] = updated
        self._persist('save_deployment', updated)
        if deployment.pipeline_run_id is not None:
            run = self._pipeline_runs[deployment.pipeline_run_id]
            pipeline_status = (
                PipelineStatus.SUCCEEDED
                if target_status == DeploymentStatus.HEALTHY
                else PipelineStatus.FAILED
            )
            self._pipeline_runs[run.id] = replace(run, status=pipeline_status, updated_at=now)
            self._persist('save_pipeline_run', self._pipeline_runs[run.id])
            self._persist('save_audit_event', f'deployment.{target_status.value}', updated, None, run.correlation_id)
            if message:
                self._pipeline_logs[run.id].append(f"deployment={target_status.value} {message}")
        return updated

    def rollback_deployment(self, deployment_id: UUID, target_artifact_digest: str) -> Deployment:
        deployment = self._deployments.get(deployment_id)
        if deployment is None:
            raise DeliveryError("DEPLOYMENT_NOT_FOUND", "deployment not found", 404)
        if deployment.status not in {DeploymentStatus.HEALTHY, DeploymentStatus.FAILED}:
            raise DeliveryError(
                "INVALID_DEPLOYMENT_STATE",
                "only a healthy or failed deployment can be rolled back",
                409,
            )
        if not IMMUTABLE_DIGEST.fullmatch(target_artifact_digest):
            raise DeliveryError(
                "IMMUTABLE_ARTIFACT_REQUIRED",
                "rollback requires a sha256 artifact digest",
                422,
            )

        now = datetime.now(timezone.utc)
        updated = replace(
            deployment,
            status=DeploymentStatus.ROLLED_BACK,
            previous_artifact_digest=deployment.artifact_digest,
            artifact_digest=target_artifact_digest,
            updated_at=now,
        )
        self._deployments[updated.id] = updated
        self._persist('save_deployment', updated)
        self._persist('save_audit_event', 'deployment.rolled_back', updated, None, None)
        if deployment.pipeline_run_id is not None:
            run = self._pipeline_runs[deployment.pipeline_run_id]
            self._pipeline_runs[run.id] = replace(
                run,
                status=PipelineStatus.ROLLED_BACK,
                updated_at=now,
            )
            self._persist('save_pipeline_run', self._pipeline_runs[run.id])
        return updated
