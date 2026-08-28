"""Application-layer module for the netCI delivery reference implementation.

The domain owns state transitions, invariants, idempotency and the source events the
DORA projection reads.  Everything that touches the outside world sits behind a seam:

* `CiLauncher`        -- who actually runs CI (nothing, or a real Jenkins controller)
* `CdOrchestrator`    -- who actually runs the long CD process (nothing, or Temporal)
* `PostgresDeliveryStore` -- where state, events, audit and logs are durably written

That is why the same rules hold whether the platform is driven by unit tests, by the
Portal against in-memory state, or by a full Ubuntu stack.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from uuid import UUID

from .adapters.cd_orchestrator import (
    CdOrchestrator,
    CdStartError,
    CdStartRequest,
    NullCdOrchestrator,
)
from .adapters.ci_launcher import CiLaunchError, CiLaunchRequest, CiLauncher, NullCiLauncher
from .persistence import (
    AuditRecord,
    ConcurrentModification,
    IdempotencyRow,
    PostgresDeliveryStore,
    UnitOfWork,
)
from .policy.rules import PolicyDecision, evaluate_artifact_evidence
from .domain.models import (
    Application,
    DeliveryEvent,
    DeliveryEventType,
    Deployment,
    DeploymentStatus,
    Environment,
    PipelineRun,
    PipelineStatus,
    Runtime,
)


logger = logging.getLogger(__name__)

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


def _now() -> datetime:
    return datetime.now(timezone.utc)


class DeliveryPlatform:
    """Own delivery state and rules behind one application-layer interface."""

    def __init__(
        self,
        ci_launcher: CiLauncher | None = None,
        cd_orchestrator: CdOrchestrator | None = None,
    ) -> None:
        self.ci_launcher: CiLauncher = ci_launcher or NullCiLauncher()
        self.cd_orchestrator: CdOrchestrator = cd_orchestrator or NullCdOrchestrator()
        self._applications: dict[UUID, Application] = {}
        self._pipeline_runs: dict[UUID, PipelineRun] = {}
        self._deployments: dict[UUID, Deployment] = {}
        self._pipeline_logs: dict[UUID, list[str]] = {}
        self._delivery_events: list[DeliveryEvent] = []
        self._security_evidence: dict[UUID, dict[str, object]] = {}
        self._idempotency_records: dict[tuple[object, ...], IdempotencyRecord] = {}
        self._store = PostgresDeliveryStore.from_env()
        self._load_persistent_state()

    # ------------------------------------------------------------- persistence

    def _load_persistent_state(self) -> None:
        if self._store is None:
            return
        data = self._store.load()
        if not data:
            return
        self._applications = {item.id: item for item in data["applications"]}
        self._pipeline_runs = {item.id: item for item in data["runs"]}
        self._deployments = {item.id: item for item in data["deployments"]}
        self._delivery_events = list(data.get("events", []))
        logs = data.get("logs") or {}
        self._pipeline_logs = {item.id: list(logs.get(item.id, [])) for item in data["runs"]}
        self._rehydrate_idempotency(data.get("idempotency") or [])

    def _rehydrate_idempotency(self, rows: list[IdempotencyRow]) -> None:
        """Rebuild replay records so a restart cannot double-create a resource."""

        for row in rows:
            if row.resource_type == "application":
                result: Application | PipelineRun | None = self._applications.get(row.resource_id)
                scope: tuple[object, ...] = ("application.create", row.idempotency_key)
            elif row.resource_type == "pipeline_run":
                result = self._pipeline_runs.get(row.resource_id)
                if result is None:
                    continue
                scope = ("pipeline.start", result.application_id, row.idempotency_key)
            else:
                continue
            if result is not None:
                self._idempotency_records[scope] = IdempotencyRecord(row.request_hash, result)

    def _commit(self, unit: UnitOfWork) -> None:
        """Persist a unit of work, then apply it to the in-memory projection.

        Persisting first means a storage failure never leaves the API reporting a
        state the database does not hold (fail-closed).
        """

        if self._store is not None:
            try:
                self._store.commit(unit)
            except ConcurrentModification as exc:
                raise DeliveryError(
                    "CONCURRENT_MODIFICATION",
                    f"record changed while this request was in flight: {exc}",
                    409,
                ) from exc
            except Exception as exc:
                raise DeliveryError("PERSISTENCE_UNAVAILABLE", f"cannot persist delivery state: {exc}", 503) from exc

        for application in unit.applications:
            self._applications[application.id] = application
        for run, expected_version in unit.runs:
            if expected_version is not None:
                current = self._pipeline_runs.get(run.id)
                if current is not None and current.version != expected_version:
                    raise DeliveryError(
                        "CONCURRENT_MODIFICATION",
                        f"pipeline run {run.id} changed while this request was in flight",
                        409,
                    )
            self._pipeline_runs[run.id] = run
            self._pipeline_logs.setdefault(run.id, [])
        for deployment, expected_version in unit.deployments:
            if expected_version is not None:
                current_deployment = self._deployments.get(deployment.id)
                if current_deployment is not None and current_deployment.version != expected_version:
                    raise DeliveryError(
                        "CONCURRENT_MODIFICATION",
                        f"deployment {deployment.id} changed while this request was in flight",
                        409,
                    )
            self._deployments[deployment.id] = deployment
        for run_id, lines in unit.logs:
            self._pipeline_logs.setdefault(run_id, []).extend(lines)
        self._delivery_events.extend(unit.events)

    def persistence_health(self) -> str:
        if self._store is None:
            return "in-memory"
        return self._store.health()

    def reset(self) -> None:
        """Clear the local adapter; intended for tests and explicit local reset."""

        self._applications.clear()
        self._pipeline_runs.clear()
        self._deployments.clear()
        self._pipeline_logs.clear()
        self._delivery_events.clear()
        self._security_evidence.clear()
        self._idempotency_records.clear()
        self._load_persistent_state()

    # --------------------------------------------------------------- catalogue

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

    # ------------------------------------------------------------ applications

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
            raise DeliveryError("RUNTIME_TEMPLATE_MISMATCH", "runtime does not match pipeline template", 422)
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
        unit = UnitOfWork(applications=[application])
        unit.audit.append(AuditRecord("application.created", application_id=application.id))
        if idempotency_key is not None:
            unit.idempotency.append(
                IdempotencyRow(
                    scope="application.create",
                    idempotency_key=idempotency_key,
                    request_hash=self._payload_hash(request_payload),
                    resource_type="application",
                    resource_id=application.id,
                    response_status=201,
                )
            )
        self._commit(unit)
        self._remember(scope, idempotency_key, request_payload, application)
        return application

    def list_applications(self) -> tuple[Application, ...]:
        return tuple(self._applications.values())

    def get_application(self, application_id: UUID) -> Application:
        application = self._applications.get(application_id)
        if application is None:
            raise DeliveryError("APPLICATION_NOT_FOUND", "application not found", 404)
        return application

    # -------------------------------------------------------------- pipelines

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
        started_by: str | None = None,
    ) -> PipelineRun:
        application = self._applications.get(application_id)
        if application is None:
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
            started_by=started_by,
        )
        unit = UnitOfWork(runs=[(run, None)])
        unit.logs.append(
            (
                run.id,
                [
                    f"queued correlationId={correlation_id}",
                    f"commit={run.commit_sha}",
                    f"startedBy={started_by or 'unknown'}",
                ],
            )
        )
        # The commit fact is what Lead Time for Changes is measured from; it is written
        # in the same transaction as the run so the metric can never lose its origin.
        unit.events.append(
            DeliveryEvent(
                event_type=DeliveryEventType.COMMIT,
                application_id=application_id,
                commit_sha=commit_sha,
                pipeline_run_id=run.id,
                environment=environment,
                occurred_at=self._commit_timestamp(parameters, run.created_at),
            )
        )
        unit.audit.append(
            AuditRecord(
                "pipeline.started",
                application_id=application_id,
                pipeline_run_id=run.id,
                correlation_id=correlation_id,
            )
        )
        if idempotency_key is not None:
            unit.idempotency.append(
                IdempotencyRow(
                    scope="pipeline.start",
                    idempotency_key=idempotency_key,
                    request_hash=self._payload_hash(request_payload),
                    resource_type="pipeline_run",
                    resource_id=run.id,
                    response_status=202,
                )
            )
        self._commit(unit)
        self._remember(scope, idempotency_key, request_payload, run)
        return self._launch_ci(application, run)

    def _launch_ci(self, application: Application, run: PipelineRun) -> PipelineRun:
        """Hand the queued run to the configured CI engine and record its identity."""

        request = CiLaunchRequest(
            application_id=application.id,
            application_name=application.name,
            repository_url=application.repository_url,
            pipeline_template=application.pipeline_template,
            runtime=application.runtime.value,
            stages=application.stages,
            pipeline_run_id=run.id,
            commit_sha=run.commit_sha,
            branch=run.branch,
            environment=run.environment.value,
            correlation_id=run.correlation_id or "",
            parameters=dict(run.parameters),
        )
        try:
            launched = self.ci_launcher.launch(request)
        except CiLaunchError as exc:
            # No engine took the build: fail the run instead of leaving it queued forever.
            failed = replace(
                run,
                status=PipelineStatus.FAILED,
                version=run.version + 1,
                updated_at=_now(),
            )
            unit = UnitOfWork(runs=[(failed, run.version)])
            unit.logs.append((run.id, [f"ci-launch-failed: {exc}"]))
            unit.audit.append(
                AuditRecord(
                    "pipeline.launch_failed",
                    application_id=application.id,
                    pipeline_run_id=run.id,
                    correlation_id=run.correlation_id,
                    payload={"error": str(exc)},
                )
            )
            self._commit(unit)
            raise DeliveryError("CI_LAUNCH_FAILED", str(exc), 502) from exc
        if launched is None:
            return run

        updated = replace(
            run,
            jenkins_run_id=launched.jenkins_run_id,
            version=run.version + 1,
            updated_at=_now(),
        )
        unit = UnitOfWork(runs=[(updated, run.version)])
        unit.logs.append(
            (run.id, [f"ci-dispatched controller={launched.controller_id} run={launched.external_run_id}"])
        )
        unit.audit.append(
            AuditRecord(
                "pipeline.dispatched",
                application_id=application.id,
                pipeline_run_id=run.id,
                correlation_id=run.correlation_id,
                payload={"controllerId": launched.controller_id, "externalRunId": launched.external_run_id},
            )
        )
        self._commit(unit)
        return updated

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

    def delivery_events(self, application_id: UUID | None = None) -> tuple[DeliveryEvent, ...]:
        """The only source the DORA projection is allowed to read."""

        if application_id is None:
            return tuple(self._delivery_events)
        return tuple(item for item in self._delivery_events if item.application_id == application_id)

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

    # ------------------------------------------------------------- invariants

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

    @staticmethod
    def _commit_timestamp(parameters: dict[str, object], fallback: datetime) -> datetime:
        """Use the real authoring time when the caller supplies it, else the queue time."""

        raw = parameters.get("commitTimestamp")
        if isinstance(raw, str):
            try:
                parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except ValueError:
                return fallback
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        return fallback

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
            raise DeliveryError("IDEMPOTENCY_KEY_REUSED", "same key was used with a different request", 409)
        if isinstance(record.result, PipelineRun):
            # Return the current state of the run, not the snapshot taken at first use.
            return self._pipeline_runs.get(record.result.id, record.result)
        return record.result

    def _remember(
        self,
        scope: tuple[object, ...],
        idempotency_key: str | None,
        request_payload: object,
        result: Application | PipelineRun,
    ) -> None:
        if idempotency_key is not None:
            self._idempotency_records[scope] = IdempotencyRecord(self._payload_hash(request_payload), result)

    @staticmethod
    def _payload_hash(payload: object) -> str:
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(canonical.encode()).hexdigest()

    # ------------------------------------------------------ supply-chain policy

    @staticmethod
    def security_evidence_required() -> bool:
        """Whether a deploy is refused when CI published no evidence at all.

        Off by default so the local reference implementation is usable without a
        full supply-chain stack; the Ubuntu acceptance profile turns it on, which
        is what makes the deny test meaningful.
        """

        return os.getenv("NETCI_REQUIRE_SECURITY_EVIDENCE", "false").strip().lower() in {"1", "true", "yes"}

    def record_security_evidence(self, pipeline_run_id: UUID, evidence: dict[str, object]) -> PolicyDecision:
        """Store the CI supply-chain evidence for a run and return the policy verdict."""

        run = self.get_pipeline(pipeline_run_id)
        digest = evidence.get("artifactDigest")
        if not isinstance(digest, str) or not IMMUTABLE_DIGEST.fullmatch(digest):
            raise DeliveryError(
                "IMMUTABLE_ARTIFACT_REQUIRED", "security evidence requires a sha256 artifact digest", 422
            )
        stored = {**evidence, "pipelineRunId": str(run.id), "applicationId": str(run.application_id)}
        decision = evaluate_artifact_evidence(stored, expected_digest=digest, require_evidence=True)
        stored["decision"] = "allow" if decision.allowed else "deny"
        stored["reason"] = decision.reason
        self._security_evidence[run.id] = stored

        unit = UnitOfWork()
        unit.logs.append((run.id, [f"security-evidence decision={stored['decision']} reason={decision.reason}"]))
        unit.audit.append(
            AuditRecord(
                "artifact.evidence_recorded",
                application_id=run.application_id,
                pipeline_run_id=run.id,
                correlation_id=run.correlation_id,
                payload=decision.as_json(),
            )
        )
        self._commit(unit)
        return decision

    def security_evidence(self, pipeline_run_id: UUID) -> dict[str, object]:
        evidence = self._security_evidence.get(pipeline_run_id)
        if evidence is None:
            raise DeliveryError("EVIDENCE_NOT_FOUND", "no security evidence for this pipeline run", 404)
        return dict(evidence)

    def _enforce_artifact_policy(self, run: PipelineRun, artifact_digest: str) -> PolicyDecision:
        """Refuse to move an artifact that cannot prove where it came from."""

        decision = evaluate_artifact_evidence(
            self._security_evidence.get(run.id),
            expected_digest=artifact_digest,
            require_evidence=self.security_evidence_required(),
        )
        unit = UnitOfWork()
        unit.audit.append(
            AuditRecord(
                "artifact.policy_evaluated",
                application_id=run.application_id,
                pipeline_run_id=run.id,
                correlation_id=run.correlation_id,
                payload=decision.as_json(),
            )
        )
        unit.logs.append((run.id, [f"policy={'allow' if decision.allowed else 'deny'} {decision.reason}"]))
        self._commit(unit)
        return decision

    # ------------------------------------------------------------- CI results

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
            updated = replace(
                run, status=PipelineStatus.RUNNING, version=run.version + 1, updated_at=_now()
            )
            unit = UnitOfWork(runs=[(updated, run.version)])
            if log_lines:
                unit.logs.append((run.id, list(log_lines)))
            self._commit(unit)
            return CiResult(updated)

        if result_status not in {PipelineStatus.SUCCEEDED.value, PipelineStatus.FAILED.value}:
            raise DeliveryError("INVALID_CI_RESULT", "CI result status is not supported", 422)
        if run.status != PipelineStatus.RUNNING:
            raise DeliveryError("INVALID_PIPELINE_STATE", "pipeline run is not running", 409)

        if result_status == PipelineStatus.FAILED.value:
            updated = replace(
                run, status=PipelineStatus.FAILED, version=run.version + 1, updated_at=_now()
            )
            unit = UnitOfWork(runs=[(updated, run.version)])
            if log_lines:
                unit.logs.append((run.id, list(log_lines)))
            unit.audit.append(
                AuditRecord(
                    "pipeline.failed",
                    application_id=run.application_id,
                    pipeline_run_id=run.id,
                    correlation_id=run.correlation_id,
                )
            )
            self._commit(unit)
            return CiResult(updated)

        if not artifact_digest or not IMMUTABLE_DIGEST.fullmatch(artifact_digest):
            raise DeliveryError(
                "IMMUTABLE_ARTIFACT_REQUIRED", "successful CI requires a sha256 artifact digest", 422
            )

        decision = self._enforce_artifact_policy(run, artifact_digest)
        if not decision.allowed:
            failed = replace(
                run, status=PipelineStatus.FAILED, artifact_digest=artifact_digest,
                version=run.version + 1, updated_at=_now(),
            )
            unit = UnitOfWork(runs=[(failed, run.version)])
            if log_lines:
                unit.logs.append((run.id, list(log_lines)))
            unit.audit.append(
                AuditRecord(
                    "artifact.policy_denied",
                    application_id=run.application_id,
                    pipeline_run_id=run.id,
                    correlation_id=run.correlation_id,
                    payload=decision.as_json(),
                )
            )
            self._commit(unit)
            raise DeliveryError("ARTIFACT_POLICY_DENIED", decision.reason, 422)

        application = self._applications[run.application_id]
        requires_approval = run.environment == Environment.PROD
        deployment = Deployment(
            application_id=run.application_id,
            pipeline_run_id=run.id,
            runtime=application.runtime,
            environment=run.environment,
            artifact_digest=artifact_digest,
            status=(DeploymentStatus.PENDING_APPROVAL if requires_approval else DeploymentStatus.DEPLOYING),
        )
        updated = replace(
            run,
            status=(PipelineStatus.WAITING_APPROVAL if requires_approval else PipelineStatus.RUNNING),
            artifact_digest=artifact_digest,
            version=run.version + 1,
            updated_at=_now(),
        )
        unit = UnitOfWork(runs=[(updated, run.version)], deployments=[(deployment, None)])
        if log_lines:
            unit.logs.append((run.id, list(log_lines)))
        unit.audit.append(
            AuditRecord(
                "pipeline.succeeded",
                application_id=run.application_id,
                pipeline_run_id=run.id,
                deployment_id=deployment.id,
                correlation_id=run.correlation_id,
                payload={"artifactDigest": artifact_digest},
            )
        )
        self._commit(unit)

        # A production deployment starts its durable workflow only after approval;
        # anything else can begin immediately.
        started = self._start_cd(application, updated, deployment)
        return CiResult(self._pipeline_runs[updated.id], started)

    def _start_cd(self, application: Application, run: PipelineRun, deployment: Deployment) -> Deployment:
        """Start the durable CD workflow for a deployment that is ready to move."""

        if deployment.status != DeploymentStatus.DEPLOYING:
            return deployment
        request = CdStartRequest(
            application_id=application.id,
            pipeline_run_id=run.id,
            deployment_id=deployment.id,
            runtime=deployment.runtime.value,
            environment=deployment.environment.value,
            artifact_digest=deployment.artifact_digest,
            release_name=application.name,
            require_approval=False,
            parameters=dict(run.parameters),
        )
        try:
            workflow_id = self.cd_orchestrator.start(request)
        except CdStartError as exc:
            failed = replace(
                deployment, status=DeploymentStatus.FAILED, version=deployment.version + 1, updated_at=_now()
            )
            failed_run = replace(run, status=PipelineStatus.FAILED, version=run.version + 1, updated_at=_now())
            unit = UnitOfWork(runs=[(failed_run, run.version)], deployments=[(failed, deployment.version)])
            unit.logs.append((run.id, [f"cd-start-failed: {exc}"]))
            unit.audit.append(
                AuditRecord(
                    "deployment.start_failed",
                    application_id=application.id,
                    pipeline_run_id=run.id,
                    deployment_id=deployment.id,
                    payload={"error": str(exc)},
                )
            )
            self._commit(unit)
            raise DeliveryError("CD_START_FAILED", str(exc), 502) from exc
        if workflow_id is None:
            return deployment

        updated_run = replace(run, workflow_id=workflow_id, version=run.version + 1, updated_at=_now())
        unit = UnitOfWork(runs=[(updated_run, run.version)])
        unit.logs.append((run.id, [f"cd-workflow-started workflowId={workflow_id}"]))
        unit.audit.append(
            AuditRecord(
                "deployment.workflow_started",
                application_id=application.id,
                pipeline_run_id=run.id,
                deployment_id=deployment.id,
                payload={"workflowId": workflow_id},
            )
        )
        self._commit(unit)
        return deployment

    # -------------------------------------------------------------- approvals

    def deployment_requested_by(self, deployment_id: UUID) -> str:
        """The subject that started the run this deployment came from, or "".

        Returns "" when the run predates authentication or the deployment was not created
        from a run. `require_separation_of_duties` reads that as "cannot be shown to be
        the same person" and permits the approval, rather than blocking every deployment
        that existed before the upgrade.
        """

        deployment = self._deployments.get(deployment_id)
        if deployment is None or deployment.pipeline_run_id is None:
            return ""
        run = self._pipeline_runs.get(deployment.pipeline_run_id)
        return (run.started_by or "") if run else ""

    def approve_deployment(self, deployment_id: UUID, actor: str) -> Deployment:
        deployment = self._deployments.get(deployment_id)
        if deployment is None:
            raise DeliveryError("DEPLOYMENT_NOT_FOUND", "deployment not found", 404)
        if deployment.status != DeploymentStatus.PENDING_APPROVAL:
            raise DeliveryError("INVALID_DEPLOYMENT_STATE", "deployment is not waiting for approval", 409)

        now = _now()
        updated = replace(
            deployment,
            status=DeploymentStatus.DEPLOYING,
            approved_by=actor,
            version=deployment.version + 1,
            updated_at=now,
        )
        unit = UnitOfWork(deployments=[(updated, deployment.version)])
        unit.audit.append(
            AuditRecord(
                "deployment.approved",
                application_id=deployment.application_id,
                pipeline_run_id=deployment.pipeline_run_id,
                deployment_id=deployment.id,
                actor=actor,
            )
        )
        run: PipelineRun | None = None
        if deployment.pipeline_run_id is not None:
            run = self._pipeline_runs[deployment.pipeline_run_id]
            resumed = replace(run, status=PipelineStatus.RUNNING, version=run.version + 1, updated_at=now)
            unit.runs.append((resumed, run.version))
            unit.logs.append((run.id, [f"approved by={actor}"]))
        self._commit(unit)

        if run is not None:
            self._resume_cd_after_approval(self._pipeline_runs[run.id], updated, actor)
        return self._deployments[updated.id]

    def _resume_cd_after_approval(self, run: PipelineRun, deployment: Deployment, actor: str) -> None:
        """Signal the waiting workflow, or start one if approval came before it existed."""

        if run.workflow_id:
            try:
                self.cd_orchestrator.signal_approval(run.workflow_id, actor, "approved via netCI")
            except CdStartError as exc:
                raise DeliveryError("CD_SIGNAL_FAILED", str(exc), 502) from exc
            return
        application = self._applications[deployment.application_id]
        self._start_cd(application, run, deployment)

    # ------------------------------------------------------ deployment results

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

        now = _now()
        target_status = DeploymentStatus(result_status)
        healthy = target_status == DeploymentStatus.HEALTHY
        updated = replace(
            deployment, status=target_status, version=deployment.version + 1, updated_at=now
        )
        unit = UnitOfWork(deployments=[(updated, deployment.version)])
        run = self._pipeline_runs.get(deployment.pipeline_run_id) if deployment.pipeline_run_id else None
        if run is not None:
            pipeline_status = PipelineStatus.SUCCEEDED if healthy else PipelineStatus.FAILED
            unit.runs.append(
                (replace(run, status=pipeline_status, version=run.version + 1, updated_at=now), run.version)
            )
            unit.audit.append(
                AuditRecord(
                    f"deployment.{target_status.value}",
                    application_id=deployment.application_id,
                    pipeline_run_id=run.id,
                    deployment_id=deployment.id,
                    correlation_id=run.correlation_id,
                )
            )
            if message:
                unit.logs.append((run.id, [f"deployment={target_status.value} {message}"]))
        self._record_delivery_outcome(unit, updated, run, healthy=healthy, occurred_at=now)
        self._commit(unit)
        return updated

    def rollback_deployment(self, deployment_id: UUID, target_artifact_digest: str) -> Deployment:
        deployment = self._deployments.get(deployment_id)
        if deployment is None:
            raise DeliveryError("DEPLOYMENT_NOT_FOUND", "deployment not found", 404)
        if deployment.status not in {DeploymentStatus.HEALTHY, DeploymentStatus.FAILED}:
            raise DeliveryError(
                "INVALID_DEPLOYMENT_STATE", "only a healthy or failed deployment can be rolled back", 409
            )
        if not IMMUTABLE_DIGEST.fullmatch(target_artifact_digest):
            raise DeliveryError("IMMUTABLE_ARTIFACT_REQUIRED", "rollback requires a sha256 artifact digest", 422)

        now = _now()
        was_healthy = deployment.status == DeploymentStatus.HEALTHY
        updated = replace(
            deployment,
            status=DeploymentStatus.ROLLED_BACK,
            previous_artifact_digest=deployment.artifact_digest,
            artifact_digest=target_artifact_digest,
            version=deployment.version + 1,
            updated_at=now,
        )
        unit = UnitOfWork(deployments=[(updated, deployment.version)])
        unit.audit.append(
            AuditRecord(
                "deployment.rolled_back",
                application_id=deployment.application_id,
                pipeline_run_id=deployment.pipeline_run_id,
                deployment_id=deployment.id,
                payload={"targetArtifactDigest": target_artifact_digest},
            )
        )
        run = self._pipeline_runs.get(deployment.pipeline_run_id) if deployment.pipeline_run_id else None
        if run is not None:
            unit.runs.append(
                (
                    replace(run, status=PipelineStatus.ROLLED_BACK, version=run.version + 1, updated_at=now),
                    run.version,
                )
            )
            unit.logs.append((run.id, [f"rolled back to {target_artifact_digest}"]))

        if deployment.environment == Environment.PROD:
            if was_healthy:
                # A healthy release that had to be rolled back is a change failure.
                unit.events.append(
                    DeliveryEvent(
                        event_type=DeliveryEventType.DEPLOYMENT,
                        application_id=deployment.application_id,
                        commit_sha=run.commit_sha if run else None,
                        pipeline_run_id=deployment.pipeline_run_id,
                        deployment_id=deployment.id,
                        environment=deployment.environment,
                        successful=False,
                        requires_intervention=True,
                        occurred_at=now,
                    )
                )
            else:
                # The rollback itself restored service for the failure already recorded.
                unit.events.append(
                    DeliveryEvent(
                        event_type=DeliveryEventType.RECOVERY,
                        application_id=deployment.application_id,
                        commit_sha=run.commit_sha if run else None,
                        pipeline_run_id=deployment.pipeline_run_id,
                        deployment_id=deployment.id,
                        environment=deployment.environment,
                        successful=True,
                        occurred_at=now,
                    )
                )
        self._commit(unit)
        return updated

    def _record_delivery_outcome(
        self,
        unit: UnitOfWork,
        deployment: Deployment,
        run: PipelineRun | None,
        *,
        healthy: bool,
        occurred_at: datetime,
    ) -> None:
        """Emit the production facts DORA is projected from.

        Only production deployments count, and a recovery is bound to the specific
        failed deployment it restored -- never inferred from ordering alone.
        """

        if deployment.environment != Environment.PROD:
            return
        unit.events.append(
            DeliveryEvent(
                event_type=DeliveryEventType.DEPLOYMENT,
                application_id=deployment.application_id,
                commit_sha=run.commit_sha if run else None,
                pipeline_run_id=deployment.pipeline_run_id,
                deployment_id=deployment.id,
                environment=deployment.environment,
                successful=healthy,
                requires_intervention=not healthy,
                occurred_at=occurred_at,
            )
        )
        if not healthy:
            return
        for failure in self._unrecovered_failures(deployment.application_id, deployment.environment):
            unit.events.append(
                DeliveryEvent(
                    event_type=DeliveryEventType.RECOVERY,
                    application_id=deployment.application_id,
                    commit_sha=failure.commit_sha,
                    pipeline_run_id=failure.pipeline_run_id,
                    deployment_id=failure.deployment_id,
                    environment=deployment.environment,
                    successful=True,
                    occurred_at=occurred_at,
                )
            )

    def _unrecovered_failures(self, application_id: UUID, environment: Environment) -> list[DeliveryEvent]:
        recovered = {
            event.deployment_id
            for event in self._delivery_events
            if event.event_type == DeliveryEventType.RECOVERY
            and event.application_id == application_id
            and event.environment == environment
        }
        return [
            event
            for event in self._delivery_events
            if event.event_type == DeliveryEventType.DEPLOYMENT
            and event.application_id == application_id
            and event.environment == environment
            and event.requires_intervention
            and event.deployment_id not in recovered
        ]
