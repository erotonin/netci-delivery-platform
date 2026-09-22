"""PostgreSQL implementation of the platform store.

Everything a request reads is fetched with a filter, inside the request's own
transaction. There is no `load()` that pulls the database into dictionaries, because
a dictionary filled at startup is a second source of truth that nothing invalidates:
a second replica would answer from a snapshot of the moment it booted.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import queue
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from ..domain.models import (
    Application,
    ConfigRevisionStatus,
    DeliveryEvent,
    DeliveryEventType,
    Deployment,
    DeploymentStatus,
    Environment,
    ModuleConfigRevision,
    NotificationRecord,
    NotificationStatus,
    PipelineRun,
    PipelineStage,
    PipelineStatus,
    Runtime,
    ScmIntegration,
    ScmProviderType,
    ScmWebhookDelivery,
    SecurityWaiver,
    ServerHealthRecord,
    ServerMaintenanceState,
    ServerTelemetry,
    StageDefinition,
    WaiverStatus,
)
from ..domain.models import AgentCommand, AgentConnection
from ..persistence import (
    AuditRecord,
    ConcurrentModification,
    IdempotencyRow,
    StillReferenced,
    UnitOfWork,
    VersionConflict,
)
from .records import (
    BreakGlassRecord,
    CatalogServiceRecord,
    CatalogTemplateRecord,
    DeploymentLease,
    ModuleRow,
    PolicyDecisionRecord,
    PreviewEnvironmentRecord,
    RequestModuleRow,
    RequestRow,
    ResourceQuotaRecord,
    ResourceRequestRecord,
    SecurityExceptionRecord,
    ServiceDependencyRecord,
    SystemRow,
    VersionRow,
)

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:  # pragma: no cover - optional dependency for source-only tests
    psycopg = None
    dict_row = None


logger = logging.getLogger(__name__)

APPLICATION_COLUMNS = (
    "id, name, repository_url, pipeline_template, runtime, default_environment,"
    " stages, owner_team, created_at, stage_parameters"
)
RUN_COLUMNS = (
    "id, application_id, status, commit_sha, branch, environment, parameters, correlation_id,"
    " jenkins_run_id, workflow_id, artifact_digest, started_by, console_url, retry_of,"
    " config_revision_id, version, created_at, updated_at"
)
STAGE_COLUMNS = (
    "id, pipeline_run_id, stage_id, stage_name, attempt, status, queued_at, started_at,"
    " completed_at, duration_ms, error_message, log_snippet, created_at, updated_at"
)
SCM_INTEGRATION_COLUMNS = (
    "id, application_id, provider, repository_identity, secret_token, secret_token_hash,"
    " credential_reference, enabled, created_at, updated_at"
)
SCM_DELIVERY_COLUMNS = (
    "delivery_id, provider, event_type, repository_identity, application_id, commit_sha,"
    " status, received_at"
)
DEPLOYMENT_COLUMNS = (
    "id, application_id, pipeline_run_id, runtime, environment, status, artifact_digest,"
    " previous_artifact_digest, approved_by, fencing_token, config_revision_id,"
    " strategy, traffic_weight, active_color, canary_step, version, created_at, updated_at"
)
EVENT_COLUMNS = (
    "id, event_type, application_id, pipeline_run_id, deployment_id, commit_sha, environment,"
    " successful, requires_intervention, occurred_at"
)
AUDIT_COLUMNS = (
    "id, event_type, application_id, pipeline_run_id, deployment_id, actor, correlation_id,"
    " payload, occurred_at"
)
MODULE_COLUMNS = (
    "id, system_id, application_id, runtime, name, module_type, description,"
    " deployment_config, pipeline_config, active_config_revision_id, config_version"
)
REQUEST_COLUMNS = (
    "id, module_id, version, requested_by, scheduled_for, rollback_strategy,"
    " run_automation_tests, status, deployment_id, comment, idempotency_key, request_hash,"
    " release_plan, strategy, strategy_config, created_at"
)
CONFIG_REVISION_COLUMNS = (
    "id, module_id, revision_number, pipeline_config, deployment_config, change_summary,"
    " status, created_by, approved_by, approved_at, rejection_reason, created_at"
)
SERVER_HEALTH_COLUMNS = (
    "server_name, status, source, freshness_seconds, details, observed_at"
)
NOTIFICATION_COLUMNS = (
    "id, event_type, aggregate_type, aggregate_id, payload, recipient, status, "
    "attempt, max_attempts, last_attempt_at, next_attempt_at, last_error, created_at, delivered_at"
)
POLICY_DECISION_COLUMNS = (
    "id, scope, target_type, target_id, allowed, reason, risk_score, checks, rules_evaluated, evaluator, evaluated_at, metadata"
)
SECURITY_EXCEPTION_COLUMNS = (
    "id, cve, artifact_digest, owner, reason, approved_by, status, created_at, expires_at, revoked_at, revoked_by"
)
BREAK_GLASS_COLUMNS = (
    "id, target_type, target_id, requested_by, reason, incident_ticket, status, approved_by, created_at, approved_at, expires_at"
)
RESOURCE_QUOTA_COLUMNS = (
    "id, scope, scope_id, max_concurrent_pipelines, max_concurrent_deployments, max_production_requests_per_day, created_at, updated_at"
)
CATALOG_SERVICE_COLUMNS = (
    "id, name, description, owning_team, tier, lifecycle, repo_url, docs_url, metadata, created_at, updated_at"
)
SERVICE_DEPENDENCY_COLUMNS = (
    "id, source_service_id, target_service_id, dependency_type, description, created_at"
)
CATALOG_TEMPLATE_COLUMNS = (
    "id, version, name, description, category, parameters_schema, pipeline_definition, is_deprecated, created_at, updated_at"
)
PREVIEW_ENVIRONMENT_COLUMNS = (
    "id, application_id, pull_request_id, commit_sha, namespace, url, status, ttl_seconds, expires_at, created_by, created_at, destroyed_at"
)
RESOURCE_REQUEST_COLUMNS = (
    "id, application_id, team_id, environment, resource_type, spec, status, status_reason, provider, outputs, requested_by, approved_by, created_at, updated_at"
)
SECURITY_WAIVER_COLUMNS = (
    "id, cve_id, module_id, reason, approved_by, status, expires_at, created_at"
)
AGENT_COMMAND_COLUMNS = "id, hostname, command, requested_by, status, result, created_at, expires_at, claimed_by, claimed_at, completed_at"


def _agent_command(row: dict[str, Any]) -> AgentCommand:
    result = row["result"]
    if isinstance(result, str):
        result = json.loads(result)
    return AgentCommand(
        id=row["id"], hostname=row["hostname"], command=row["command"], requested_by=row["requested_by"],
        status=row["status"], result=result, created_at=row["created_at"], expires_at=row["expires_at"],
        claimed_by=row["claimed_by"], claimed_at=row["claimed_at"], completed_at=row["completed_at"],
    )


STAGE_CATALOG_COLUMNS = (
    "id, name, category, description, kind, script, after_stage, required, enabled_by_default,"
    " position, created_by, created_at, updated_at, status, approved_by, parameters"
)
SERVER_MAINTENANCE_COLUMNS = (
    "server_name, in_maintenance, reason, updated_by, updated_at"
)


def encode_cursor(timestamp: datetime, record_id: UUID | str) -> str:
    payload = f"{timestamp.isoformat()}|{record_id}"
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii")


def decode_cursor(cursor_str: str | None) -> tuple[datetime, str] | None:
    if not cursor_str:
        return None
    try:
        raw = base64.urlsafe_b64decode(cursor_str.encode("ascii")).decode("utf-8")
        ts_str, id_str = raw.split("|", 1)
        parsed = datetime.fromisoformat(ts_str)
        # A cursor this server minted is always tz-aware. One that is not was crafted,
        # and the two stores disagreed about it in silence: PostgreSQL compared it as a
        # local timestamp, the in-memory store raised and swallowed it and handed back
        # page one forever. Reading it as UTC makes both answer the same thing.
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed, id_str
    except Exception:
        return None


def _notification(row: dict[str, Any]) -> NotificationRecord:
    return NotificationRecord(
        id=row["id"],
        event_type=row["event_type"],
        aggregate_type=row["aggregate_type"],
        aggregate_id=row["aggregate_id"],
        payload=dict(row["payload"] or {}),
        recipient=row["recipient"],
        status=NotificationStatus(row["status"]),
        attempt=int(row["attempt"] or 0),
        max_attempts=int(row["max_attempts"] or 5),
        last_attempt_at=row["last_attempt_at"],
        next_attempt_at=row["next_attempt_at"],
        last_error=row["last_error"],
        created_at=row["created_at"],
        delivered_at=row["delivered_at"],
    )


def _policy_decision(row: dict[str, Any]) -> PolicyDecisionRecord:
    return PolicyDecisionRecord(
        id=row["id"],
        scope=row["scope"],
        target_type=row["target_type"],
        target_id=row["target_id"],
        allowed=bool(row["allowed"]),
        reason=row["reason"],
        risk_score=int(row["risk_score"] or 0),
        checks=dict(row["checks"] or {}),
        rules_evaluated=list(row["rules_evaluated"] or []),
        evaluator=row["evaluator"],
        evaluated_at=row["evaluated_at"],
        metadata=dict(row["metadata"] or {}),
    )


def _security_exception(row: dict[str, Any]) -> SecurityExceptionRecord:
    return SecurityExceptionRecord(
        id=row["id"],
        cve=row["cve"],
        artifact_digest=row["artifact_digest"],
        owner=row["owner"],
        reason=row["reason"],
        approved_by=row["approved_by"],
        status=row["status"],
        created_at=row["created_at"],
        expires_at=row["expires_at"],
        revoked_at=row["revoked_at"],
        revoked_by=row["revoked_by"],
    )


def _security_waiver(row: dict[str, Any]) -> SecurityWaiver:
    return SecurityWaiver(
        id=UUID(str(row["id"])),
        cve_id=row["cve_id"],
        module_id=row.get("module_id"),
        reason=row["reason"],
        approved_by=row["approved_by"],
        status=WaiverStatus(row["status"]),
        expires_at=row["expires_at"],
        created_at=row["created_at"],
    )


def _stage_definition(row: dict[str, Any]) -> StageDefinition:
    return StageDefinition(
        id=row["id"], name=row["name"], category=row["category"], description=row.get("description") or "",
        kind=row["kind"], script=row.get("script"), after_stage=row.get("after_stage"),
        required=bool(row["required"]), enabled_by_default=bool(row["enabled_by_default"]),
        position=int(row["position"]), created_by=row.get("created_by") or "netci",
        created_at=row["created_at"], updated_at=row["updated_at"],
        status=row.get("status") or "active", approved_by=row.get("approved_by"),
        parameters=tuple(row.get("parameters") or ()),
    )


def _server_maintenance(row: dict[str, Any]) -> ServerMaintenanceState:
    return ServerMaintenanceState(
        server_name=row["server_name"],
        in_maintenance=bool(row["in_maintenance"]),
        reason=row.get("reason") or "",
        updated_by=row.get("updated_by") or "operator",
        updated_at=row["updated_at"],
    )


def _break_glass(row: dict[str, Any]) -> BreakGlassRecord:
    return BreakGlassRecord(
        id=row["id"],
        target_type=row["target_type"],
        target_id=row["target_id"],
        requested_by=row["requested_by"],
        reason=row["reason"],
        incident_ticket=row["incident_ticket"],
        status=row["status"],
        created_at=row["created_at"],
        approved_by=row["approved_by"],
        approved_at=row["approved_at"],
        expires_at=row["expires_at"],
    )


def _resource_quota(row: dict[str, Any]) -> ResourceQuotaRecord:
    return ResourceQuotaRecord(
        id=row["id"],
        scope=row["scope"],
        scope_id=row["scope_id"],
        max_concurrent_pipelines=int(row["max_concurrent_pipelines"]),
        max_concurrent_deployments=int(row["max_concurrent_deployments"]),
        max_production_requests_per_day=int(row["max_production_requests_per_day"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _catalog_service(row: dict[str, Any]) -> CatalogServiceRecord:
    return CatalogServiceRecord(
        id=row["id"],
        name=row["name"],
        description=row["description"] or "",
        owning_team=row["owning_team"],
        tier=row["tier"],
        lifecycle=row["lifecycle"],
        repo_url=row["repo_url"] or "",
        docs_url=row["docs_url"] or "",
        metadata=dict(row["metadata"] or {}),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _service_dependency(row: dict[str, Any]) -> ServiceDependencyRecord:
    return ServiceDependencyRecord(
        id=row["id"],
        source_service_id=row["source_service_id"],
        target_service_id=row["target_service_id"],
        dependency_type=row["dependency_type"],
        description=row["description"] or "",
        created_at=row["created_at"],
    )


def _catalog_template(row: dict[str, Any]) -> CatalogTemplateRecord:
    return CatalogTemplateRecord(
        id=row["id"],
        version=row["version"],
        name=row["name"],
        description=row["description"] or "",
        category=row["category"],
        parameters_schema=dict(row["parameters_schema"] or {}),
        pipeline_definition=dict(row["pipeline_definition"] or {}),
        is_deprecated=bool(row["is_deprecated"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _preview_environment(row: dict[str, Any]) -> PreviewEnvironmentRecord:
    return PreviewEnvironmentRecord(
        id=row["id"],
        application_id=row["application_id"],
        pull_request_id=row["pull_request_id"],
        commit_sha=row["commit_sha"],
        namespace=row["namespace"],
        url=row["url"],
        status=row["status"],
        ttl_seconds=int(row["ttl_seconds"]),
        expires_at=row["expires_at"],
        created_by=row["created_by"],
        created_at=row["created_at"],
        destroyed_at=row["destroyed_at"],
    )


def _resource_request(row: dict[str, Any]) -> ResourceRequestRecord:
    return ResourceRequestRecord(
        id=row["id"],
        application_id=row["application_id"],
        team_id=row["team_id"],
        environment=row["environment"],
        resource_type=row["resource_type"],
        spec=dict(row["spec"] or {}),
        status=row["status"],
        status_reason=row["status_reason"] or "",
        provider=row["provider"],
        outputs=dict(row["outputs"] or {}),
        requested_by=row["requested_by"],
        approved_by=row["approved_by"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )




def _application(row: dict[str, Any]) -> Application:
    return Application(
        name=row["name"],
        repository_url=row["repository_url"],
        pipeline_template=row["pipeline_template"],
        runtime=Runtime(row["runtime"]),
        default_environment=Environment(row["default_environment"]),
        stages=tuple(row["stages"] or []),
        owner_team=row["owner_team"],
        stage_parameters=dict(row.get("stage_parameters") or {}),
        id=row["id"],
        created_at=row["created_at"],
    )


def _run(row: dict[str, Any]) -> PipelineRun:
    return PipelineRun(
        id=row["id"],
        application_id=row["application_id"],
        status=PipelineStatus(row["status"]),
        commit_sha=row["commit_sha"],
        branch=row["branch"],
        environment=Environment(row["environment"]),
        parameters=dict(row["parameters"] or {}),
        correlation_id=row["correlation_id"] or "",
        jenkins_run_id=row["jenkins_run_id"],
        workflow_id=row["workflow_id"],
        artifact_digest=row["artifact_digest"],
        started_by=row["started_by"],
        console_url=row.get("console_url"),
        retry_of=row.get("retry_of"),
        config_revision_id=row.get("config_revision_id"),
        version=int(row["version"] or 1),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _stage(row: dict[str, Any]) -> PipelineStage:
    return PipelineStage(
        id=row["id"],
        pipeline_run_id=row["pipeline_run_id"],
        stage_id=row["stage_id"],
        stage_name=row["stage_name"],
        attempt=int(row["attempt"] or 1),
        status=row["status"],
        queued_at=row.get("queued_at"),
        started_at=row.get("started_at"),
        completed_at=row.get("completed_at"),
        duration_ms=row.get("duration_ms"),
        error_message=row.get("error_message"),
        log_snippet=row.get("log_snippet"),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _scm_integration(row: dict[str, Any]) -> ScmIntegration:
    return ScmIntegration(
        id=row["id"],
        application_id=row["application_id"],
        provider=ScmProviderType(row["provider"]),
        repository_identity=row["repository_identity"],
        secret_token=row.get("secret_token"),
        secret_token_hash=row.get("secret_token_hash"),
        credential_reference=row.get("credential_reference"),
        enabled=bool(row["enabled"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _scm_delivery(row: dict[str, Any]) -> ScmWebhookDelivery:
    return ScmWebhookDelivery(
        delivery_id=row["delivery_id"],
        provider=ScmProviderType(row["provider"]),
        event_type=row["event_type"],
        repository_identity=row["repository_identity"],
        status=row["status"],
        application_id=row.get("application_id"),
        commit_sha=row.get("commit_sha"),
        received_at=row["received_at"],
    )


def _deployment(row: dict[str, Any]) -> Deployment:
    return Deployment(
        id=row["id"],
        application_id=row["application_id"],
        pipeline_run_id=row["pipeline_run_id"],
        runtime=Runtime(row["runtime"]),
        environment=Environment(row["environment"]),
        status=DeploymentStatus(row["status"]),
        artifact_digest=row["artifact_digest"],
        previous_artifact_digest=row["previous_artifact_digest"],
        approved_by=row["approved_by"],
        fencing_token=int(row["fencing_token"]) if row["fencing_token"] is not None else None,
        config_revision_id=row.get("config_revision_id"),
        strategy=str(row.get("strategy") or "rolling"),
        traffic_weight=int(row.get("traffic_weight") if row.get("traffic_weight") is not None else 100),
        active_color=row.get("active_color"),
        canary_step=int(row.get("canary_step") or 0),
        version=int(row["version"] or 1),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _event(row: dict[str, Any]) -> DeliveryEvent:
    return DeliveryEvent(
        id=row["id"],
        event_type=DeliveryEventType(row["event_type"]),
        application_id=row["application_id"],
        pipeline_run_id=row["pipeline_run_id"],
        deployment_id=row["deployment_id"],
        commit_sha=row["commit_sha"],
        environment=Environment(row["environment"]) if row["environment"] else None,
        successful=row["successful"],
        requires_intervention=bool(row["requires_intervention"]),
        occurred_at=row["occurred_at"],
    )


def _audit(row: dict[str, Any]) -> AuditRecord:
    return AuditRecord(
        id=row["id"],
        event_type=row["event_type"],
        application_id=row["application_id"],
        pipeline_run_id=row["pipeline_run_id"],
        deployment_id=row["deployment_id"],
        actor=row["actor"],
        correlation_id=row["correlation_id"],
        payload=dict(row["payload"] or {}),
        occurred_at=row["occurred_at"],
    )


def _module(row: dict[str, Any]) -> ModuleRow:
    application_id = row["application_id"]
    if application_id is not None and not isinstance(application_id, UUID):
        application_id = UUID(str(application_id))
    active_rev_id = row.get("active_config_revision_id")
    if active_rev_id is not None and not isinstance(active_rev_id, UUID):
        active_rev_id = UUID(str(active_rev_id))
    return ModuleRow(
        id=str(row["id"]),
        system_id=str(row["system_id"]),
        name=str(row["name"]),
        module_type=str(row["module_type"]),
        description=str(row["description"] or ""),
        runtime=str(row["runtime"] or "docker"),
        application_id=application_id,
        deployment_config=list(row["deployment_config"] or []),
        pipeline_config=dict(row["pipeline_config"] or {}),
        active_config_revision_id=active_rev_id,
        config_version=int(row.get("config_version") or 1),
    )


def _config_revision(row: dict[str, Any]) -> ModuleConfigRevision:
    return ModuleConfigRevision(
        id=row["id"],
        module_id=str(row["module_id"]),
        revision_number=int(row["revision_number"]),
        pipeline_config=dict(row["pipeline_config"] or {}),
        deployment_config=list(row["deployment_config"] or []),
        change_summary=str(row.get("change_summary") or ""),
        status=ConfigRevisionStatus(row["status"]),
        created_by=str(row["created_by"]),
        approved_by=row.get("approved_by"),
        approved_at=row.get("approved_at"),
        rejection_reason=row.get("rejection_reason"),
        created_at=row["created_at"],
    )


def _server_health(row: dict[str, Any]) -> ServerHealthRecord:
    return ServerHealthRecord(
        server_name=str(row["server_name"]),
        status=str(row["status"]),
        source=str(row["source"]),
        freshness_seconds=int(row.get("freshness_seconds") or 0),
        details=dict(row.get("details") or {}),
        observed_at=row["observed_at"],
    )


class PostgresSession:
    """One transaction. Reads are filtered queries; writes enforce the read version."""

    def __init__(self, cursor) -> None:
        self._cursor = cursor

    # ------------------------------------------------------------- delivery reads

    def application(self, application_id: UUID) -> Application | None:
        self._cursor.execute(
            f"SELECT {APPLICATION_COLUMNS} FROM applications WHERE id = %s", (application_id,)
        )
        row = self._cursor.fetchone()
        return _application(row) if row else None

    def application_by_name(self, name: str) -> Application | None:
        self._cursor.execute(
            f"SELECT {APPLICATION_COLUMNS} FROM applications WHERE name = %s", (name,)
        )
        row = self._cursor.fetchone()
        return _application(row) if row else None

    def applications(self) -> tuple[Application, ...]:
        self._cursor.execute(f"SELECT {APPLICATION_COLUMNS} FROM applications ORDER BY created_at, id")
        return tuple(_application(row) for row in self._cursor.fetchall())

    def pipeline_run(self, pipeline_run_id: UUID) -> PipelineRun | None:
        self._cursor.execute(
            f"SELECT {RUN_COLUMNS} FROM pipeline_runs WHERE id = %s", (pipeline_run_id,)
        )
        row = self._cursor.fetchone()
        return _run(row) if row else None

    def pipeline_runs(self, application_id: UUID | None = None) -> tuple[PipelineRun, ...]:
        if application_id is None:
            self._cursor.execute(f"SELECT {RUN_COLUMNS} FROM pipeline_runs ORDER BY created_at, id")
        else:
            self._cursor.execute(
                f"SELECT {RUN_COLUMNS} FROM pipeline_runs WHERE application_id = %s ORDER BY created_at, id",
                (application_id,),
            )
        return tuple(_run(row) for row in self._cursor.fetchall())

    def pipeline_run_by_artifact_digest(self, artifact_digest: str) -> PipelineRun | None:
        """The run that produced a digest, by index.

        Kubernetes admission asks this for every container of every pod. It used to be
        answered by loading every pipeline_runs row and scanning the list in Python, so
        the cost grew with the table that grows fastest -- and an admission webhook that
        runs out of time either blocks the pod or, under failurePolicy=Ignore, admits it
        without the check. A digest identifies one artifact, so the newest run carrying
        it is the answer.
        """

        self._cursor.execute(
            f"SELECT {RUN_COLUMNS} FROM pipeline_runs WHERE artifact_digest = %s"
            " ORDER BY created_at DESC, id DESC LIMIT 1",
            (artifact_digest,),
        )
        row = self._cursor.fetchone()
        return _run(row) if row else None

    def digest_in_service(self, application_id: UUID, environment: str) -> str | None:
        """The digest this environment is running, as far as netCI has established.

        The newest deployment that ended with a release serving traffic: `healthy`, or
        `rolled_back` (which serves the digest it restored). Asked on every deployment
        netCI creates, and answered by loading every deployment the application ever had
        until migration 0027 gave it an index.
        """

        self._cursor.execute(
            "SELECT artifact_digest FROM deployments"
            " WHERE application_id = %s AND environment = %s"
            "   AND status IN ('healthy', 'rolled_back')"
            " ORDER BY updated_at DESC LIMIT 1",
            (application_id, environment),
        )
        row = self._cursor.fetchone()
        return row["artifact_digest"] if row else None

    def runs_awaiting_ci_result(self, limit: int = 50) -> tuple[PipelineRun, ...]:
        """The runs the reconciler has to ask Jenkins about, filtered and capped in SQL.

        The reconciler used to load every pipeline_runs row and pick the active ones in
        Python, applying its limit afterwards. Nothing thins that table -- retention
        covers console lines, delivery events, notifications and spent callback tokens,
        not the runs themselves -- so a loop that runs on a timer forever read a table
        that only grows, every cycle. The predicate is the same one it applied: a run
        carrying a digest has already had its CI result, and re-reporting it would build
        a second deployment for the same artifact.
        """

        self._cursor.execute(
            f"SELECT {RUN_COLUMNS} FROM pipeline_runs"
            " WHERE status = ANY(%s) AND artifact_digest IS NULL"
            " ORDER BY created_at, id LIMIT %s",
            ([PipelineStatus.QUEUED.value, PipelineStatus.RUNNING.value], limit),
        )
        return tuple(_run(row) for row in self._cursor.fetchall())

    def deployments_with_status(
        self, status: DeploymentStatus, limit: int = 50
    ) -> tuple[Deployment, ...]:
        self._cursor.execute(
            f"SELECT {DEPLOYMENT_COLUMNS} FROM deployments WHERE status = %s"
            " ORDER BY created_at, id LIMIT %s",
            (status.value, limit),
        )
        return tuple(_deployment(row) for row in self._cursor.fetchall())

    def deployment(self, deployment_id: UUID) -> Deployment | None:
        self._cursor.execute(
            f"SELECT {DEPLOYMENT_COLUMNS} FROM deployments WHERE id = %s", (deployment_id,)
        )
        row = self._cursor.fetchone()
        return _deployment(row) if row else None

    def deployments(
        self,
        application_id: UUID | None = None,
        pipeline_run_id: UUID | None = None,
    ) -> tuple[Deployment, ...]:
        clauses: list[str] = []
        arguments: list[Any] = []
        if application_id is not None:
            clauses.append("application_id = %s")
            arguments.append(application_id)
        if pipeline_run_id is not None:
            clauses.append("pipeline_run_id = %s")
            arguments.append(pipeline_run_id)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        self._cursor.execute(
            f"SELECT {DEPLOYMENT_COLUMNS} FROM deployments{where} ORDER BY created_at, id",
            tuple(arguments),
        )
        return tuple(_deployment(row) for row in self._cursor.fetchall())

    def pipeline_logs(self, pipeline_run_id: UUID) -> tuple[str, ...]:
        self._cursor.execute(
            "SELECT line FROM pipeline_logs WHERE pipeline_run_id = %s ORDER BY sequence",
            (pipeline_run_id,),
        )
        return tuple(row["line"] for row in self._cursor.fetchall())

    def delivery_events(self, application_id: UUID | None = None) -> tuple[DeliveryEvent, ...]:
        if application_id is None:
            self._cursor.execute(
                f"SELECT {EVENT_COLUMNS} FROM delivery_events ORDER BY occurred_at, id"
            )
        else:
            self._cursor.execute(
                f"SELECT {EVENT_COLUMNS} FROM delivery_events WHERE application_id = %s"
                " ORDER BY occurred_at, id",
                (application_id,),
            )
        return tuple(_event(row) for row in self._cursor.fetchall())

    def security_evidence(self, pipeline_run_id: UUID) -> dict[str, Any] | None:
        self._cursor.execute(
            "SELECT evidence FROM security_evidence WHERE pipeline_run_id = %s", (pipeline_run_id,)
        )
        row = self._cursor.fetchone()
        return dict(row["evidence"] or {}) if row else None

    def audit_records(self, application_ids: set[UUID] | None = None) -> tuple[AuditRecord, ...]:
        if application_ids is None:
            self._cursor.execute(
                f"SELECT {AUDIT_COLUMNS} FROM audit_events ORDER BY occurred_at DESC, id DESC"
            )
        elif not application_ids:
            return ()
        else:
            self._cursor.execute(
                f"SELECT {AUDIT_COLUMNS} FROM audit_events WHERE application_id = ANY(%s)"
                " ORDER BY occurred_at DESC, id DESC",
                (list(application_ids),),
            )
        return tuple(_audit(row) for row in self._cursor.fetchall())

    def idempotency(self, scope: str, idempotency_key: str) -> IdempotencyRow | None:
        self._cursor.execute(
            "SELECT scope, idempotency_key, request_hash, resource_type, resource_id, response_status"
            " FROM idempotency_records WHERE scope = %s AND idempotency_key = %s",
            (scope, idempotency_key),
        )
        row = self._cursor.fetchone()
        if row is None:
            return None
        return IdempotencyRow(
            scope=row["scope"],
            idempotency_key=row["idempotency_key"],
            request_hash=row["request_hash"],
            resource_type=row["resource_type"],
            resource_id=row["resource_id"],
            response_status=int(row["response_status"]),
        )

    def claim_callback_token(
        self,
        *,
        jti: str,
        workload: str,
        application_id: UUID,
        operation: str,
        expires_at: datetime,
        pipeline_run_id: UUID | None = None,
        deployment_id: UUID | None = None,
    ) -> bool:
        """Claim a token's `jti`. False means it was already spent.

        `ON CONFLICT DO NOTHING` makes the claim atomic: two replicas handed the same
        replayed token cannot both see it as unused, because the primary key decides.
        """

        self._cursor.execute(
            """
            INSERT INTO callback_token_uses (
                jti, workload, application_id, pipeline_run_id, deployment_id, operation, expires_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (jti) DO NOTHING
            RETURNING jti
            """,
            (jti, workload, application_id, pipeline_run_id, deployment_id, operation, expires_at),
        )
        return self._cursor.fetchone() is not None

    def callback_token_used(self, jti: str) -> bool:
        self._cursor.execute("SELECT 1 FROM callback_token_uses WHERE jti = %s", (jti,))
        return self._cursor.fetchone() is not None

    # -------------------------------------------------------------------- leases

    def acquire_deployment_lease(
        self,
        *,
        application_id: UUID,
        environment: str,
        target: str,
        deployment_id: UUID,
        owner: str,
        ttl_seconds: int,
        now: datetime,
    ) -> DeploymentLease | None:
        """Claim a target, or return None if someone else holds it and has not expired.

        Expiry is reclaimed here rather than by a sweeper, because the moment that matters
        is the moment somebody wants the target. A sweeper adds a window in which the lease
        is dead but still blocking, and a second thing that can be down.
        """

        self._cursor.execute(
            """
            UPDATE deployment_leases
               SET released_at = %s, release_reason = 'expired'
             WHERE application_id = %s AND environment = %s AND target = %s
               AND released_at IS NULL AND expires_at <= %s
            """,
            (now, application_id, environment, target, now),
        )
        reclaimed = self._cursor.rowcount

        # The counter is what keeps fencing tokens rising across releases: a new lease
        # must never reissue a number a stale workflow still holds. The UPDATE takes a row
        # lock, so two replicas racing here serialise on it.
        self._cursor.execute(
            """
            INSERT INTO deployment_fencing_counters (application_id, environment, target, next_token)
            VALUES (%s, %s, %s, 1)
            ON CONFLICT (application_id, environment, target) DO NOTHING
            """,
            (application_id, environment, target),
        )
        self._cursor.execute(
            """
            UPDATE deployment_fencing_counters
               SET next_token = next_token + 1
             WHERE application_id = %s AND environment = %s AND target = %s
            RETURNING next_token - 1 AS token
            """,
            (application_id, environment, target),
        )
        token = int(self._cursor.fetchone()["token"])

        expires_at = now + timedelta(seconds=max(1, ttl_seconds))
        # `ON CONFLICT ... DO NOTHING` rather than catching the violation: a raised
        # UniqueViolation aborts the whole PostgreSQL transaction, so every statement
        # after it fails with "current transaction is aborted" -- including the audit
        # record explaining the conflict. Letting the index decide silently keeps the
        # transaction usable, which is what makes a refusal reportable.
        self._cursor.execute(
            """
            INSERT INTO deployment_leases (
                application_id, environment, target, deployment_id, owner,
                fencing_token, acquired_at, heartbeat_at, expires_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (application_id, environment, target) WHERE released_at IS NULL
            DO NOTHING
            RETURNING id
            """,
            (application_id, environment, target, deployment_id, owner, token,
             now, now, expires_at),
        )
        row = self._cursor.fetchone()
        if row is None:
            # Someone else holds an unexpired lease. The partial unique index decided
            # that, not a read this replica did a moment ago.
            return None
        lease_id = row["id"]
        _ = reclaimed
        return DeploymentLease(
            id=lease_id,
            application_id=application_id,
            environment=environment,
            target=target,
            deployment_id=deployment_id,
            owner=owner,
            fencing_token=token,
            acquired_at=now,
            heartbeat_at=now,
            expires_at=expires_at,
        )

    def active_deployment_lease(
        self, *, application_id: UUID, environment: str, target: str
    ) -> DeploymentLease | None:
        self._cursor.execute(
            """
            SELECT id, application_id, environment, target, deployment_id, owner, fencing_token,
                   acquired_at, heartbeat_at, expires_at, released_at, release_reason
              FROM deployment_leases
             WHERE application_id = %s AND environment = %s AND target = %s AND released_at IS NULL
            """,
            (application_id, environment, target),
        )
        row = self._cursor.fetchone()
        return DeploymentLease(**row) if row else None

    def deployment_lease(self, deployment_id: UUID) -> DeploymentLease | None:
        self._cursor.execute(
            """
            SELECT id, application_id, environment, target, deployment_id, owner, fencing_token,
                   acquired_at, heartbeat_at, expires_at, released_at, release_reason
              FROM deployment_leases
             WHERE deployment_id = %s
             ORDER BY acquired_at DESC LIMIT 1
            """,
            (deployment_id,),
        )
        row = self._cursor.fetchone()
        return DeploymentLease(**row) if row else None

    def heartbeat_deployment_lease(
        self, lease_id: UUID, *, ttl_seconds: int, now: datetime
    ) -> bool:
        """Extend a lease this owner still holds. False means it was lost or released."""

        self._cursor.execute(
            """
            UPDATE deployment_leases
               SET heartbeat_at = %s, expires_at = %s
             WHERE id = %s AND released_at IS NULL
            """,
            (now, now + timedelta(seconds=max(1, ttl_seconds)), lease_id),
        )
        return self._cursor.rowcount == 1

    def release_deployment_lease(self, lease_id: UUID, *, reason: str, now: datetime) -> bool:
        self._cursor.execute(
            """
            UPDATE deployment_leases
               SET released_at = %s, release_reason = %s
             WHERE id = %s AND released_at IS NULL
            """,
            (now, reason[:64], lease_id),
        )
        return self._cursor.rowcount == 1

    def expired_deployment_leases(self, now: datetime, limit: int = 100) -> tuple[DeploymentLease, ...]:
        self._cursor.execute(
            """
            SELECT id, application_id, environment, target, deployment_id, owner, fencing_token,
                   acquired_at, heartbeat_at, expires_at, released_at, release_reason
              FROM deployment_leases
             WHERE released_at IS NULL AND expires_at <= %s
             ORDER BY expires_at LIMIT %s
            """,
            (now, limit),
        )
        return tuple(DeploymentLease(**row) for row in self._cursor.fetchall())

    # ------------------------------------------------------------ delivery writes

    def apply(self, unit: UnitOfWork) -> None:
        if unit.is_empty():
            return
        cursor = self._cursor
        for application in unit.applications:
            cursor.execute(
                """
                INSERT INTO applications (id, name, repository_url, pipeline_template, runtime,
                                          default_environment, stages, owner_team, created_at, stage_parameters)
                VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s::jsonb)
                ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name, stages = EXCLUDED.stages,
                                               owner_team = EXCLUDED.owner_team,
                                               stage_parameters = EXCLUDED.stage_parameters
                """,
                (
                    application.id,
                    application.name,
                    application.repository_url,
                    application.pipeline_template,
                    application.runtime.value,
                    application.default_environment.value,
                    json.dumps(list(application.stages)),
                    application.owner_team,
                    application.created_at,
                    json.dumps(application.stage_parameters),
                ),
            )
        for run, expected_version in unit.runs:
            self._write_run(cursor, run, expected_version)
        for deployment, expected_version in unit.deployments:
            self._write_deployment(cursor, deployment, expected_version)
        for run_id, lines in unit.logs:
            if not lines:
                continue
            # `max(sequence) + 1` is not safe under concurrency: two transactions read the
            # same max and both insert it, and one dies on the primary key taking its whole
            # unit of work with it. Reserving a block with UPDATE ... RETURNING takes a row
            # lock, so each number is handed out once and ordering is preserved within the
            # block a writer reserved.
            cursor.execute(
                "INSERT INTO pipeline_log_sequences (pipeline_run_id, next_sequence)"
                " VALUES (%s, 1) ON CONFLICT (pipeline_run_id) DO NOTHING",
                (run_id,),
            )
            cursor.execute(
                "UPDATE pipeline_log_sequences SET next_sequence = next_sequence + %s"
                " WHERE pipeline_run_id = %s RETURNING next_sequence - %s AS start",
                (len(lines), run_id, len(lines)),
            )
            start = int(cursor.fetchone()["start"])
            for offset, line in enumerate(lines):
                cursor.execute(
                    "INSERT INTO pipeline_logs (pipeline_run_id, sequence, line) VALUES (%s, %s, %s)",
                    (run_id, start + offset, line[:8000]),
                )
        for event in unit.events:
            cursor.execute(
                f"""
                INSERT INTO delivery_events ({EVENT_COLUMNS})
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO NOTHING
                """,
                (
                    event.id,
                    event.event_type.value,
                    event.application_id,
                    event.pipeline_run_id,
                    event.deployment_id,
                    event.commit_sha,
                    event.environment.value if event.environment else None,
                    event.successful,
                    event.requires_intervention,
                    event.occurred_at,
                ),
            )
        for record in unit.audit:
            cursor.execute(
                f"INSERT INTO audit_events ({AUDIT_COLUMNS})"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s) ON CONFLICT (id) DO NOTHING",
                (
                    record.id,
                    record.event_type,
                    record.application_id,
                    record.pipeline_run_id,
                    record.deployment_id,
                    record.actor,
                    record.correlation_id,
                    json.dumps(record.payload, default=str),
                    record.occurred_at,
                ),
            )
        for pipeline_run_id, application_id, artifact_digest, evidence in unit.security_evidence:
            cursor.execute(
                """
                INSERT INTO security_evidence (
                    pipeline_run_id, application_id, artifact_digest, evidence, decision, reason
                ) VALUES (%s, %s, %s, %s::jsonb, %s, %s)
                ON CONFLICT (pipeline_run_id) DO UPDATE
                   SET artifact_digest = EXCLUDED.artifact_digest,
                       evidence = EXCLUDED.evidence,
                       decision = EXCLUDED.decision,
                       reason = EXCLUDED.reason,
                       updated_at = now()
                """,
                (
                    pipeline_run_id,
                    application_id,
                    artifact_digest,
                    json.dumps(evidence, default=str),
                    evidence["decision"],
                    evidence["reason"],
                ),
            )
        for notification in unit.notifications:
            self.record_notification(notification)
        for row in unit.idempotency:
            cursor.execute(
                """
                INSERT INTO idempotency_records (scope, idempotency_key, request_hash, resource_type,
                                                 resource_id, response_status, response_body)
                VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb)
                ON CONFLICT (scope, idempotency_key) DO NOTHING
                RETURNING resource_id
                """,
                (
                    row.scope,
                    row.idempotency_key,
                    row.request_hash,
                    row.resource_type,
                    row.resource_id,
                    row.response_status,
                    json.dumps({"resourceId": str(row.resource_id)}),
                ),
            )
            if cursor.fetchone() is None:
                # A competing transaction claimed this key between our read and this
                # insert. Raising rolls back every resource written above, so the retry
                # replays the winner's resource instead of creating a duplicate.
                raise ConcurrentModification(
                    f"idempotency key {row.scope}/{row.idempotency_key} was committed concurrently"
                )

    @staticmethod
    def _write_run(cursor, run: PipelineRun, expected_version: int | None) -> None:
        if expected_version is None:
            cursor.execute(
                """
                INSERT INTO pipeline_runs (id, application_id, commit_sha, branch, environment,
                                           parameters, status, jenkins_run_id, workflow_id,
                                           artifact_digest, correlation_id, started_by,
                                           console_url, retry_of, config_revision_id, version, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    run.id,
                    run.application_id,
                    run.commit_sha,
                    run.branch,
                    run.environment.value,
                    json.dumps(run.parameters, default=str),
                    run.status.value,
                    run.jenkins_run_id,
                    run.workflow_id,
                    run.artifact_digest,
                    run.correlation_id,
                    run.started_by,
                    run.console_url,
                    run.retry_of,
                    run.config_revision_id,
                    run.version,
                    run.created_at,
                    run.updated_at,
                ),
            )
            return
        cursor.execute(
            """
            UPDATE pipeline_runs
               SET status = %s, parameters = %s::jsonb, jenkins_run_id = %s, workflow_id = %s,
                   artifact_digest = %s, console_url = %s, version = %s, updated_at = %s
             WHERE id = %s AND version = %s
            """,
            (
                run.status.value,
                json.dumps(run.parameters, default=str),
                run.jenkins_run_id,
                run.workflow_id,
                run.artifact_digest,
                run.console_url,
                run.version,
                run.updated_at,
                run.id,
                expected_version,
            ),
        )
        if cursor.rowcount != 1:
            raise ConcurrentModification(f"pipeline run {run.id} changed since it was read")

    @staticmethod
    def _write_deployment(cursor, deployment: Deployment, expected_version: int | None) -> None:
        if expected_version is None:
            cursor.execute(
                """
                INSERT INTO deployments (id, application_id, pipeline_run_id, runtime, environment,
                                         status, artifact_digest, previous_artifact_digest,
                                         approved_by, fencing_token, config_revision_id,
                                         strategy, traffic_weight, active_color, canary_step,
                                         version, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    deployment.id,
                    deployment.application_id,
                    deployment.pipeline_run_id,
                    deployment.runtime.value,
                    deployment.environment.value,
                    deployment.status.value,
                    deployment.artifact_digest,
                    deployment.previous_artifact_digest,
                    deployment.approved_by,
                    deployment.fencing_token,
                    deployment.config_revision_id,
                    deployment.strategy,
                    deployment.traffic_weight,
                    deployment.active_color,
                    deployment.canary_step,
                    deployment.version,
                    deployment.created_at,
                    deployment.updated_at,
                ),
            )
            return
        cursor.execute(
            """
            UPDATE deployments
               SET status = %s, artifact_digest = %s, previous_artifact_digest = %s,
                   approved_by = %s, fencing_token = %s,
                   strategy = %s, traffic_weight = %s, active_color = %s, canary_step = %s,
                   version = %s, updated_at = %s
             WHERE id = %s AND version = %s
            """,
            (
                deployment.status.value,
                deployment.artifact_digest,
                deployment.previous_artifact_digest,
                deployment.approved_by,
                deployment.fencing_token,
                deployment.strategy,
                deployment.traffic_weight,
                deployment.active_color,
                deployment.canary_step,
                deployment.version,
                deployment.updated_at,
                deployment.id,
                expected_version,
            ),
        )
        if cursor.rowcount != 1:
            raise ConcurrentModification(f"deployment {deployment.id} changed since it was read")

    # --------------------------------------------------------------- portal reads

    def portal_system(self, system_id: str) -> SystemRow | None:
        self._cursor.execute(
            "SELECT id, unit, description, owner, status FROM systems WHERE id = %s", (system_id,)
        )
        row = self._cursor.fetchone()
        if row is None:
            return None
        return SystemRow(str(row["id"]), str(row["unit"]), str(row["description"]),
                         str(row["owner"]), str(row["status"]))

    def portal_systems(self) -> tuple[SystemRow, ...]:
        self._cursor.execute(
            "SELECT id, unit, description, owner, status FROM systems ORDER BY created_at, id"
        )
        return tuple(
            SystemRow(str(row["id"]), str(row["unit"]), str(row["description"]),
                      str(row["owner"]), str(row["status"]))
            for row in self._cursor.fetchall()
        )

    def portal_module(self, module_id: str) -> ModuleRow | None:
        self._cursor.execute(f"SELECT {MODULE_COLUMNS} FROM modules WHERE id = %s", (module_id,))
        row = self._cursor.fetchone()
        return _module(row) if row else None

    def portal_modules(self, system_id: str | None = None) -> tuple[ModuleRow, ...]:
        if system_id is None:
            self._cursor.execute(f"SELECT {MODULE_COLUMNS} FROM modules ORDER BY created_at, id")
        else:
            self._cursor.execute(
                f"SELECT {MODULE_COLUMNS} FROM modules WHERE system_id = %s ORDER BY created_at, id",
                (system_id,),
            )
        return tuple(_module(row) for row in self._cursor.fetchall())

    def portal_module_for_application(self, application_id: UUID) -> ModuleRow | None:
        self._cursor.execute(
            f"SELECT {MODULE_COLUMNS} FROM modules WHERE application_id = %s", (application_id,)
        )
        row = self._cursor.fetchone()
        return _module(row) if row else None

    def portal_versions(self, module_id: str) -> tuple[VersionRow, ...]:
        self._cursor.execute(
            """
            SELECT rv.module_id, rv.version, rv.metadata, r.report
            FROM release_versions rv
            LEFT JOIN LATERAL (
                SELECT report FROM version_ci_reports vcr
                WHERE vcr.module_id = rv.module_id AND vcr.version = rv.version
                ORDER BY vcr.recorded_at DESC, vcr.id DESC
                LIMIT 1
            ) r ON true
            WHERE rv.module_id = %s
            ORDER BY rv.created_at DESC, rv.version DESC
            """,
            (module_id,),
        )
        results: list[VersionRow] = []
        for row in self._cursor.fetchall():
            meta = dict(row["metadata"] or {})
            if row["report"] is not None:
                meta["ciReport"] = row["report"]
            results.append(VersionRow(str(row["module_id"]), str(row["version"]), meta))
        return tuple(results)

    def portal_version(self, module_id: str, version: str) -> VersionRow | None:
        self._cursor.execute(
            """
            SELECT rv.module_id, rv.version, rv.metadata, r.report
            FROM release_versions rv
            LEFT JOIN LATERAL (
                SELECT report FROM version_ci_reports vcr
                WHERE vcr.module_id = rv.module_id AND vcr.version = rv.version
                ORDER BY vcr.recorded_at DESC, vcr.id DESC
                LIMIT 1
            ) r ON true
            WHERE rv.module_id = %s AND rv.version = %s
            """,
            (module_id, version),
        )
        row = self._cursor.fetchone()
        if row is None:
            return None
        meta = dict(row["metadata"] or {})
        if row["report"] is not None:
            meta["ciReport"] = row["report"]
        return VersionRow(str(row["module_id"]), str(row["version"]), meta)

    def _requests_where(self, clause: str, arguments: tuple[Any, ...]) -> tuple[RequestRow, ...]:
        self._cursor.execute(
            f"SELECT {REQUEST_COLUMNS} FROM production_requests{clause} ORDER BY created_at, id",
            arguments,
        )
        rows = self._cursor.fetchall()
        if not rows:
            return ()
        identifiers = [row["id"] for row in rows]
        self._cursor.execute(
            "SELECT request_id, module_id, version, deployment_order, dependencies, status, deployment_id, started_at, completed_at, error_message"
            " FROM production_request_modules"
            " WHERE request_id = ANY(%s) ORDER BY request_id, deployment_order, module_id",
            (identifiers,),
        )
        members: dict[str, list[RequestModuleRow]] = {}
        for item in self._cursor.fetchall():
            members.setdefault(str(item["request_id"]), []).append(
                RequestModuleRow(
                    module_id=str(item["module_id"]),
                    version=str(item["version"]),
                    deployment_order=int(item["deployment_order"] or 1),
                    dependencies=tuple(item.get("dependencies") or ()),
                    status=str(item.get("status") or "pending"),
                    deployment_id=item.get("deployment_id"),
                    started_at=item.get("started_at"),
                    completed_at=item.get("completed_at"),
                    error_message=item.get("error_message"),
                )
            )
        output: list[RequestRow] = []
        for row in rows:
            request_id = str(row["id"])
            modules = members.get(request_id) or [
                # Requests written before multi-module support carry their single module
                # on the parent row; reading it keeps those rows addressable after upgrade.
                RequestModuleRow(str(row["module_id"]), str(row["version"] or "v0.0.0"))
            ]
            output.append(
                RequestRow(
                    id=request_id,
                    modules=tuple(modules),
                    requested_by=str(row["requested_by"]),
                    scheduled_for=row["scheduled_for"],
                    rollback_strategy=str(row["rollback_strategy"]),
                    run_automation_tests=bool(row["run_automation_tests"]),
                    status=str(row["status"]),
                    deployment_id=row["deployment_id"],
                    comment=row["comment"],
                    idempotency_key=row["idempotency_key"],
                    request_hash=row["request_hash"],
                    release_plan=row.get("release_plan"),
                    strategy=str(row.get("strategy") or "rolling"),
                    strategy_config=dict(row.get("strategy_config") or {}),
                    created_at=row.get("created_at"),
                )
            )
        return tuple(output)

    def portal_requests(self) -> tuple[RequestRow, ...]:
        return self._requests_where("", ())

    def portal_request(self, request_id: str) -> RequestRow | None:
        try:
            identifier = UUID(str(request_id))
        except ValueError:
            return None
        found = self._requests_where(" WHERE id = %s", (identifier,))
        return found[0] if found else None

    def portal_request_for_update(self, request_id: str) -> RequestRow | None:
        """The request, with its row locked until this transaction ends.

        For the one caller that reads a release plan, decides a wave is finished, and
        writes the plan back: a read-modify-write of a JSON document, which a conditional
        UPDATE cannot express. Two deployment callbacks for the last two modules of a
        wave used to arrive together, both see the wave complete, and both start the next
        one -- dispatching a production wave twice. The lock is held for the length of
        that decision and nothing else.
        """

        try:
            identifier = UUID(str(request_id))
        except ValueError:
            return None
        self._cursor.execute(
            "SELECT id FROM production_requests WHERE id = %s FOR UPDATE", (identifier,)
        )
        if self._cursor.fetchone() is None:
            return None
        found = self._requests_where(" WHERE id = %s", (identifier,))
        return found[0] if found else None

    def portal_request_by_idempotency_key(self, idempotency_key: str) -> RequestRow | None:
        found = self._requests_where(" WHERE idempotency_key = %s", (idempotency_key,))
        return found[0] if found else None

    def portal_request_for_deployment(self, deployment_id: UUID) -> RequestRow | None:
        found = self._requests_where(" WHERE deployment_id = %s", (deployment_id,))
        return found[0] if found else None

    def portal_modules_referenced_by_requests(self) -> set[str]:
        self._cursor.execute("SELECT DISTINCT module_id FROM production_request_modules")
        return {str(row["module_id"]) for row in self._cursor.fetchall()}

    # -------------------------------------------------------------- portal writes

    def insert_portal_system(self, row: SystemRow) -> None:
        self._cursor.execute(
            "INSERT INTO systems (id, unit, description, owner, status)"
            " VALUES (%s, %s, %s, %s, %s)",
            (row.id, row.unit, row.description, row.owner, row.status),
        )

    def delete_portal_system(self, system_id: str) -> None:
        try:
            self._cursor.execute("DELETE FROM modules WHERE system_id = %s", (system_id,))
            self._cursor.execute("DELETE FROM systems WHERE id = %s", (system_id,))
        except psycopg.errors.ForeignKeyViolation as exc:
            raise StillReferenced(
                f"system {system_id} has a module that a production request still references"
            ) from exc

    def insert_portal_module(self, row: ModuleRow) -> None:
        self._cursor.execute(
            f"""
            INSERT INTO modules ({MODULE_COLUMNS})
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s)
            """,
            (
                row.id,
                row.system_id,
                row.application_id,
                row.runtime,
                row.name,
                row.module_type,
                row.description,
                json.dumps(row.deployment_config, default=str),
                json.dumps(row.pipeline_config, default=str),
                row.active_config_revision_id,
                row.config_version,
            ),
        )

    def update_portal_module(
        self, module_id: str, *, name: str, module_type: str, description: str
    ) -> None:
        self._cursor.execute(
            "UPDATE modules SET name = %s, module_type = %s, description = %s WHERE id = %s",
            (name, module_type, description, module_id),
        )
        if self._cursor.rowcount != 1:
            raise KeyError("module not found")

    def delete_portal_module(self, module_id: str) -> None:
        try:
            self._cursor.execute("DELETE FROM modules WHERE id = %s", (module_id,))
        except psycopg.errors.ForeignKeyViolation as exc:
            raise StillReferenced(
                f"module {module_id} is still referenced by a production request"
            ) from exc

    def insert_portal_version(self, row: VersionRow) -> None:
        try:
            self._cursor.execute(
                """
                INSERT INTO release_versions (module_id, version, artifact_digest, metadata)
                VALUES (%s, %s, %s, %s::jsonb)
                ON CONFLICT (module_id, version) DO NOTHING
                RETURNING id
                """,
                (row.module_id, row.version, row.metadata.get("artifactDigest"), json.dumps(row.metadata, default=str)),
            )
            inserted = self._cursor.fetchone()
            if not inserted:
                existing = self.portal_version(row.module_id, row.version)
                if existing and existing.metadata == row.metadata:
                    return
                raise VersionConflict(
                    f"release version '{row.version}' already exists for module '{row.module_id}'"
                )
        except psycopg.errors.UniqueViolation as exc:
            raise VersionConflict(
                f"release version '{row.version}' already exists for module '{row.module_id}'"
            ) from exc

    def upsert_portal_version(self, row: VersionRow) -> None:
        self.insert_portal_version(row)

    def insert_version_ci_report(
        self, module_id: str, version: str, report: dict[str, Any], recorded_by: str = "netCI Pipeline"
    ) -> None:
        self._cursor.execute(
            """
            INSERT INTO version_ci_reports (module_id, version, report, recorded_by)
            VALUES (%s, %s, %s::jsonb, %s)
            """,
            (module_id, version, json.dumps(report, default=str), recorded_by),
        )

    def latest_version_ci_report(self, module_id: str, version: str) -> dict[str, Any] | None:
        self._cursor.execute(
            """
            SELECT report FROM version_ci_reports
            WHERE module_id = %s AND version = %s
            ORDER BY recorded_at DESC, id DESC
            LIMIT 1
            """,
            (module_id, version),
        )
        row = self._cursor.fetchone()
        return dict(row["report"]) if row and row["report"] else None

    def insert_portal_request(self, row: RequestRow) -> None:
        primary = row.modules[0]
        self._cursor.execute(
            """
            INSERT INTO production_requests (
                id, module_id, version, requested_by, scheduled_for,
                rollback_strategy, run_automation_tests, status, idempotency_key, request_hash,
                release_plan, strategy, strategy_config
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                UUID(str(row.id)),
                primary.module_id,
                primary.version,
                row.requested_by,
                row.scheduled_for,
                row.rollback_strategy,
                row.run_automation_tests,
                row.status,
                row.idempotency_key,
                row.request_hash,
                json.dumps(row.release_plan) if row.release_plan is not None else None,
                row.strategy,
                json.dumps(row.strategy_config),
            ),
        )
        for member in row.modules:
            self._cursor.execute(
                """
                INSERT INTO production_request_modules (
                    request_id, module_id, version, deployment_order,
                    dependencies, status, deployment_id, started_at, completed_at, error_message
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    UUID(str(row.id)),
                    member.module_id,
                    member.version,
                    member.deployment_order,
                    list(member.dependencies),
                    member.status,
                    member.deployment_id,
                    member.started_at,
                    member.completed_at,
                    member.error_message,
                ),
            )

    def update_portal_request(
        self,
        request_id: str,
        *,
        status: str,
        comment: str | None,
        deployment_id: UUID | None = None,
        release_plan: dict[str, Any] | None = None,
    ) -> None:
        if release_plan is not None:
            self._cursor.execute(
                "UPDATE production_requests SET status = %s, comment = %s,"
                " deployment_id = COALESCE(%s, deployment_id), release_plan = %s WHERE id = %s",
                (status, comment, deployment_id, json.dumps(release_plan), UUID(str(request_id))),
            )
        else:
            self._cursor.execute(
                "UPDATE production_requests SET status = %s, comment = %s,"
                " deployment_id = COALESCE(%s, deployment_id) WHERE id = %s",
                (status, comment, deployment_id, UUID(str(request_id))),
            )

    def claim_portal_request(
        self,
        request_id: str,
        *,
        from_status: str,
        to_status: str,
        comment: str | None = None,
    ) -> bool:
        """Move a request between statuses only if it is still in `from_status`.

        Reading the status and then writing it are two statements, and between them a
        second approver can read the same status and pass the same check -- which used
        to let one production request dispatch its first wave twice. The condition lives
        in the UPDATE so the database decides who won: exactly one caller sees True.
        """

        self._cursor.execute(
            "UPDATE production_requests SET status = %s,"
            " comment = COALESCE(%s, comment) WHERE id = %s AND status = %s",
            (to_status, comment, UUID(str(request_id)), from_status),
        )
        return self._cursor.rowcount == 1

    def update_portal_request_module(
        self,
        request_id: str,
        module_id: str,
        *,
        status: str,
        deployment_id: UUID | None = None,
        error_message: str | None = None,
        started_at: datetime | None = None,
        completed_at: datetime | None = None,
    ) -> None:
        self._cursor.execute(
            """
            UPDATE production_request_modules
               SET status = %s,
                   deployment_id = COALESCE(%s, deployment_id),
                   error_message = COALESCE(%s, error_message),
                   started_at = COALESCE(%s, started_at),
                   completed_at = COALESCE(%s, completed_at)
             WHERE request_id = %s AND module_id = %s
            """,
            (
                status,
                deployment_id,
                error_message,
                started_at,
                completed_at,
                UUID(str(request_id)),
                module_id,
            ),
        )

    def update_deployment_traffic(
        self,
        deployment_id: UUID,
        *,
        strategy: str | None = None,
        traffic_weight: int,
        canary_step: int = 0,
        active_color: str | None = None,
    ) -> None:
        self._cursor.execute(
            """
            UPDATE deployments
               SET strategy = COALESCE(%s, strategy),
                   traffic_weight = %s,
                   canary_step = %s,
                   active_color = COALESCE(%s, active_color),
                   updated_at = NOW()
             WHERE id = %s
            """,
            (strategy, traffic_weight, canary_step, active_color, deployment_id),
        )

    # ------------------------------------------------- SCM integrations & webhooks

    def scm_integration(self, integration_id: UUID) -> ScmIntegration | None:
        self._cursor.execute(
            f"SELECT {SCM_INTEGRATION_COLUMNS} FROM scm_integrations WHERE id = %s",
            (integration_id,),
        )
        row = self._cursor.fetchone()
        return _scm_integration(row) if row else None

    def scm_integration_for_application(
        self, application_id: UUID, provider: ScmProviderType | None = None
    ) -> ScmIntegration | None:
        if provider:
            self._cursor.execute(
                f"SELECT {SCM_INTEGRATION_COLUMNS} FROM scm_integrations WHERE application_id = %s AND provider = %s",
                (application_id, provider.value),
            )
        else:
            self._cursor.execute(
                f"SELECT {SCM_INTEGRATION_COLUMNS} FROM scm_integrations WHERE application_id = %s ORDER BY created_at LIMIT 1",
                (application_id,),
            )
        row = self._cursor.fetchone()
        return _scm_integration(row) if row else None

    def scm_integration_for_repository(
        self, provider: ScmProviderType, repository_identity: str
    ) -> ScmIntegration | None:
        self._cursor.execute(
            f"SELECT {SCM_INTEGRATION_COLUMNS} FROM scm_integrations WHERE provider = %s AND repository_identity = %s",
            (provider.value, repository_identity),
        )
        row = self._cursor.fetchone()
        return _scm_integration(row) if row else None

    def upsert_scm_integration(self, integration: ScmIntegration) -> None:
        self._cursor.execute(
            """
            INSERT INTO scm_integrations (id, application_id, provider, repository_identity,
                                         secret_token, secret_token_hash, credential_reference,
                                         enabled, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (application_id, provider) DO UPDATE
               SET repository_identity = EXCLUDED.repository_identity,
                   secret_token = COALESCE(EXCLUDED.secret_token, scm_integrations.secret_token),
                   secret_token_hash = COALESCE(EXCLUDED.secret_token_hash, scm_integrations.secret_token_hash),
                   credential_reference = EXCLUDED.credential_reference,
                   enabled = EXCLUDED.enabled,
                   updated_at = EXCLUDED.updated_at
            """,
            (
                integration.id,
                integration.application_id,
                integration.provider.value,
                integration.repository_identity,
                integration.secret_token,
                integration.secret_token_hash,
                integration.credential_reference,
                integration.enabled,
                integration.created_at,
                integration.updated_at,
            ),
        )

    def record_scm_webhook_delivery(self, delivery: ScmWebhookDelivery) -> bool:
        self._cursor.execute(
            """
            INSERT INTO scm_webhook_deliveries (delivery_id, provider, event_type,
                                                repository_identity, application_id,
                                                commit_sha, status, received_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (delivery_id) DO NOTHING
            """,
            (
                delivery.delivery_id,
                delivery.provider.value,
                delivery.event_type,
                delivery.repository_identity,
                delivery.application_id,
                delivery.commit_sha,
                delivery.status,
                delivery.received_at,
            ),
        )
        return self._cursor.rowcount > 0

    def scm_webhook_delivery(self, delivery_id: str) -> ScmWebhookDelivery | None:
        self._cursor.execute(
            f"SELECT {SCM_DELIVERY_COLUMNS} FROM scm_webhook_deliveries WHERE delivery_id = %s",
            (delivery_id,),
        )
        row = self._cursor.fetchone()
        return _scm_delivery(row) if row else None

    # ------------------------------------------------------------- pipeline stages

    def record_pipeline_stage(self, stage: PipelineStage) -> PipelineStage:
        self._cursor.execute(
            f"""
            INSERT INTO pipeline_stages (
                id, pipeline_run_id, stage_id, stage_name, attempt, status,
                queued_at, started_at, completed_at, duration_ms, error_message, log_snippet,
                created_at, updated_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (pipeline_run_id, stage_id, attempt) DO UPDATE SET
                stage_name = EXCLUDED.stage_name,
                status = EXCLUDED.status,
                queued_at = COALESCE(EXCLUDED.queued_at, pipeline_stages.queued_at),
                started_at = COALESCE(EXCLUDED.started_at, pipeline_stages.started_at),
                completed_at = COALESCE(EXCLUDED.completed_at, pipeline_stages.completed_at),
                duration_ms = COALESCE(EXCLUDED.duration_ms, pipeline_stages.duration_ms),
                error_message = COALESCE(EXCLUDED.error_message, pipeline_stages.error_message),
                log_snippet = COALESCE(EXCLUDED.log_snippet, pipeline_stages.log_snippet),
                updated_at = EXCLUDED.updated_at
            RETURNING {STAGE_COLUMNS}
            """,
            (
                stage.id,
                stage.pipeline_run_id,
                stage.stage_id,
                stage.stage_name,
                stage.attempt,
                stage.status,
                stage.queued_at,
                stage.started_at,
                stage.completed_at,
                stage.duration_ms,
                stage.error_message,
                stage.log_snippet,
                stage.created_at,
                stage.updated_at,
            ),
        )
        row = self._cursor.fetchone()
        return _stage(row)

    def pipeline_stages(self, pipeline_run_id: UUID) -> tuple[PipelineStage, ...]:
        self._cursor.execute(
            f"SELECT {STAGE_COLUMNS} FROM pipeline_stages WHERE pipeline_run_id = %s ORDER BY created_at, attempt, stage_id",
            (pipeline_run_id,),
        )
        return tuple(_stage(row) for row in self._cursor.fetchall())

    # ---------------------------------------- versioned config & server health

    def config_revisions(self, module_id: str) -> tuple[ModuleConfigRevision, ...]:
        self._cursor.execute(
            f"SELECT {CONFIG_REVISION_COLUMNS} FROM module_config_revisions WHERE module_id = %s ORDER BY revision_number DESC",
            (module_id,),
        )
        return tuple(_config_revision(row) for row in self._cursor.fetchall())

    def config_revision(self, revision_id: UUID) -> ModuleConfigRevision | None:
        self._cursor.execute(
            f"SELECT {CONFIG_REVISION_COLUMNS} FROM module_config_revisions WHERE id = %s",
            (revision_id,),
        )
        row = self._cursor.fetchone()
        return _config_revision(row) if row else None

    def config_revision_by_number(
        self, module_id: str, revision_number: int
    ) -> ModuleConfigRevision | None:
        self._cursor.execute(
            f"SELECT {CONFIG_REVISION_COLUMNS} FROM module_config_revisions WHERE module_id = %s AND revision_number = %s",
            (module_id, revision_number),
        )
        row = self._cursor.fetchone()
        return _config_revision(row) if row else None

    def active_config_revision(self, module_id: str) -> ModuleConfigRevision | None:
        self._cursor.execute(
            """
            SELECT r.* FROM module_config_revisions r
            JOIN modules m ON m.active_config_revision_id = r.id
            WHERE m.id = %s
            """,
            (module_id,),
        )
        row = self._cursor.fetchone()
        return _config_revision(row) if row else None

    def record_config_revision(
        self, revision: ModuleConfigRevision
    ) -> ModuleConfigRevision:
        self._cursor.execute(
            f"""
            INSERT INTO module_config_revisions (
                id, module_id, revision_number, pipeline_config, deployment_config,
                change_summary, status, created_by, approved_by, approved_at,
                rejection_reason, created_at
            ) VALUES (%s, %s, %s, %s::jsonb, %s::jsonb, %s, %s, %s, %s, %s, %s, %s)
            RETURNING {CONFIG_REVISION_COLUMNS}
            """,
            (
                revision.id,
                revision.module_id,
                revision.revision_number,
                json.dumps(revision.pipeline_config, default=str),
                json.dumps(revision.deployment_config, default=str),
                revision.change_summary,
                revision.status.value,
                revision.created_by,
                revision.approved_by,
                revision.approved_at,
                revision.rejection_reason,
                revision.created_at,
            ),
        )
        row = self._cursor.fetchone()
        return _config_revision(row)

    def update_config_revision_status(
        self,
        revision_id: UUID,
        status: ConfigRevisionStatus,
        approved_by: str | None = None,
        approved_at: datetime | None = None,
        rejection_reason: str | None = None,
        expected_status: ConfigRevisionStatus | None = None,
    ) -> ModuleConfigRevision | None:
        """Move a revision, optionally only from the status the caller checked.

        With `expected_status` the precondition lives in the UPDATE. Reading the status
        and then writing it are two statements, and READ COMMITTED lets a second approver
        read the same `pending_approval` between them: both passed the check, both
        activated, and activation supersedes the previous revision and sends a
        notification each time. Returning None means someone else got there first.
        """

        if expected_status is None:
            self._cursor.execute(
                f"""
                UPDATE module_config_revisions
                   SET status = %s, approved_by = COALESCE(%s, approved_by),
                       approved_at = COALESCE(%s, approved_at),
                       rejection_reason = COALESCE(%s, rejection_reason)
                 WHERE id = %s
                RETURNING {CONFIG_REVISION_COLUMNS}
                """,
                (status.value, approved_by, approved_at, rejection_reason, revision_id),
            )
        else:
            self._cursor.execute(
                f"""
                UPDATE module_config_revisions
                   SET status = %s, approved_by = COALESCE(%s, approved_by),
                       approved_at = COALESCE(%s, approved_at),
                       rejection_reason = COALESCE(%s, rejection_reason)
                 WHERE id = %s AND status = %s
                RETURNING {CONFIG_REVISION_COLUMNS}
                """,
                (status.value, approved_by, approved_at, rejection_reason,
                 revision_id, expected_status.value),
            )
        row = self._cursor.fetchone()
        return _config_revision(row) if row else None

    def replace_portal_module_config(
        self,
        module_id: str,
        *,
        deployment_config: list[dict[str, Any]],
        pipeline_config: dict[str, Any],
    ) -> None:
        self._cursor.execute(
            "UPDATE modules SET deployment_config = %s::jsonb, pipeline_config = %s::jsonb"
            " WHERE id = %s",
            (
                json.dumps(deployment_config, default=str),
                json.dumps(pipeline_config, default=str),
                module_id,
            ),
        )

    def set_module_active_revision(
        self, module_id: str, revision_id: UUID, expected_config_version: int
    ) -> bool:
        # Fetch the revision to sync pipeline_config and deployment_config to modules row
        rev = self.config_revision(revision_id)
        if not rev:
            return False
        self._cursor.execute(
            """
            UPDATE modules
               SET active_config_revision_id = %s,
                   config_version = config_version + 1,
                   pipeline_config = %s::jsonb,
                   deployment_config = %s::jsonb
             WHERE id = %s AND config_version = %s
            """,
            (
                revision_id,
                json.dumps(rev.pipeline_config, default=str),
                json.dumps(rev.deployment_config, default=str),
                module_id,
                expected_config_version,
            ),
        )
        return self._cursor.rowcount == 1

    def server_health(self, server_name: str) -> ServerHealthRecord | None:
        self._cursor.execute(
            f"SELECT {SERVER_HEALTH_COLUMNS} FROM server_health_records WHERE server_name = %s",
            (server_name,),
        )
        row = self._cursor.fetchone()
        return _server_health(row) if row else None

    def list_server_health(self) -> tuple[ServerHealthRecord, ...]:
        self._cursor.execute(
            f"SELECT {SERVER_HEALTH_COLUMNS} FROM server_health_records ORDER BY server_name"
        )
        return tuple(_server_health(row) for row in self._cursor.fetchall())

    def record_server_health(self, record: ServerHealthRecord) -> None:
        self._cursor.execute(
            """
            INSERT INTO server_health_records (
                server_name, status, source, freshness_seconds, details, observed_at
            ) VALUES (%s, %s, %s, %s, %s::jsonb, %s)
            ON CONFLICT (server_name) DO UPDATE SET
                status = EXCLUDED.status,
                source = EXCLUDED.source,
                freshness_seconds = EXCLUDED.freshness_seconds,
                details = EXCLUDED.details,
                observed_at = EXCLUDED.observed_at
            """,
            (
                record.server_name,
                record.status,
                record.source,
                record.freshness_seconds,
                json.dumps(record.details, default=str),
                record.observed_at,
            ),
        )

    # ----------------------------------------------- notifications & outbox

    def record_notification(self, notification: NotificationRecord) -> NotificationRecord:
        self._cursor.execute(
            f"""
            INSERT INTO notifications (
                id, event_type, aggregate_type, aggregate_id, payload, recipient, status,
                attempt, max_attempts, last_attempt_at, next_attempt_at, last_error, created_at, delivered_at
            ) VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING {NOTIFICATION_COLUMNS}
            """,
            (
                notification.id,
                notification.event_type,
                notification.aggregate_type,
                notification.aggregate_id,
                json.dumps(notification.payload, default=str),
                notification.recipient,
                notification.status.value,
                notification.attempt,
                notification.max_attempts,
                notification.last_attempt_at,
                notification.next_attempt_at,
                notification.last_error,
                notification.created_at,
                notification.delivered_at,
            ),
        )
        return _notification(self._cursor.fetchone())

    def notification(self, notification_id: UUID) -> NotificationRecord | None:
        self._cursor.execute(
            f"SELECT {NOTIFICATION_COLUMNS} FROM notifications WHERE id = %s", (notification_id,)
        )
        row = self._cursor.fetchone()
        return _notification(row) if row else None

    def pending_notifications(
        self, limit: int = 100, now: datetime | None = None
    ) -> tuple[NotificationRecord, ...]:
        ts = now or datetime.now(timezone.utc)
        self._cursor.execute(
            f"""
            SELECT {NOTIFICATION_COLUMNS} FROM notifications
            WHERE status IN ('pending', 'failed') AND next_attempt_at <= %s
            ORDER BY next_attempt_at, id
            LIMIT %s
            """,
            (ts, limit),
        )
        return tuple(_notification(row) for row in self._cursor.fetchall())

    def update_notification_status(
        self,
        notification_id: UUID,
        status: NotificationStatus,
        attempt: int,
        next_attempt_at: datetime,
        last_error: str | None = None,
        delivered_at: datetime | None = None,
    ) -> NotificationRecord | None:
        self._cursor.execute(
            f"""
            UPDATE notifications
            SET status = %s,
                attempt = %s,
                next_attempt_at = %s,
                last_attempt_at = %s,
                last_error = %s,
                delivered_at = COALESCE(%s, delivered_at)
            WHERE id = %s
            RETURNING {NOTIFICATION_COLUMNS}
            """,
            (
                status.value,
                attempt,
                next_attempt_at,
                datetime.now(timezone.utc),
                last_error,
                delivered_at,
                notification_id,
            ),
        )
        row = self._cursor.fetchone()
        return _notification(row) if row else None

    def notifications_paginated(
        self, status: NotificationStatus | None = None, limit: int = 50, cursor: str | None = None
    ) -> tuple[tuple[NotificationRecord, ...], str | None, bool]:
        clauses: list[str] = []
        arguments: list[Any] = []
        if status is not None:
            clauses.append("status = %s")
            arguments.append(status.value)
        decoded = decode_cursor(cursor)
        if decoded is not None:
            ts, record_id = decoded
            clauses.append("(created_at, id) < (%s, %s)")
            arguments.extend([ts, record_id])
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        query = f"SELECT {NOTIFICATION_COLUMNS} FROM notifications{where} ORDER BY created_at DESC, id DESC LIMIT %s"
        arguments.append(limit + 1)
        self._cursor.execute(query, tuple(arguments))
        rows = self._cursor.fetchall()
        has_more = len(rows) > limit
        result_rows = rows[:limit]
        items = tuple(_notification(row) for row in result_rows)
        next_cursor = encode_cursor(items[-1].created_at, items[-1].id) if (has_more and items) else None
        return items, next_cursor, has_more

    # --------------------------------------------------- cursor pagination

    def pipeline_runs_paginated(
        self, application_id: UUID | None = None, limit: int = 50, cursor: str | None = None
    ) -> tuple[tuple[PipelineRun, ...], str | None, bool]:
        clauses: list[str] = []
        arguments: list[Any] = []
        if application_id is not None:
            clauses.append("application_id = %s")
            arguments.append(application_id)
        decoded = decode_cursor(cursor)
        if decoded is not None:
            ts, record_id = decoded
            clauses.append("(created_at, id) < (%s, %s)")
            arguments.extend([ts, record_id])
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        query = f"SELECT {RUN_COLUMNS} FROM pipeline_runs{where} ORDER BY created_at DESC, id DESC LIMIT %s"
        arguments.append(limit + 1)
        self._cursor.execute(query, tuple(arguments))
        rows = self._cursor.fetchall()
        has_more = len(rows) > limit
        result_rows = rows[:limit]
        items = tuple(_run(row) for row in result_rows)
        next_cursor = encode_cursor(items[-1].created_at, items[-1].id) if (has_more and items) else None
        return items, next_cursor, has_more

    def deployments_paginated(
        self, application_id: UUID | None = None, limit: int = 50, cursor: str | None = None
    ) -> tuple[tuple[Deployment, ...], str | None, bool]:
        clauses: list[str] = []
        arguments: list[Any] = []
        if application_id is not None:
            clauses.append("application_id = %s")
            arguments.append(application_id)
        decoded = decode_cursor(cursor)
        if decoded is not None:
            ts, record_id = decoded
            clauses.append("(created_at, id) < (%s, %s)")
            arguments.extend([ts, record_id])
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        query = f"SELECT {DEPLOYMENT_COLUMNS} FROM deployments{where} ORDER BY created_at DESC, id DESC LIMIT %s"
        arguments.append(limit + 1)
        self._cursor.execute(query, tuple(arguments))
        rows = self._cursor.fetchall()
        has_more = len(rows) > limit
        result_rows = rows[:limit]
        items = tuple(_deployment(row) for row in result_rows)
        next_cursor = encode_cursor(items[-1].created_at, items[-1].id) if (has_more and items) else None
        return items, next_cursor, has_more

    def audit_records_paginated(
        self, application_id: UUID | None = None, limit: int = 50, cursor: str | None = None
    ) -> tuple[tuple[AuditRecord, ...], str | None, bool]:
        clauses: list[str] = []
        arguments: list[Any] = []
        if application_id is not None:
            clauses.append("application_id = %s")
            arguments.append(application_id)
        decoded = decode_cursor(cursor)
        if decoded is not None:
            ts, record_id = decoded
            clauses.append("(occurred_at, id) < (%s, %s)")
            arguments.extend([ts, record_id])
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        query = f"SELECT {AUDIT_COLUMNS} FROM audit_events{where} ORDER BY occurred_at DESC, id DESC LIMIT %s"
        arguments.append(limit + 1)
        self._cursor.execute(query, tuple(arguments))
        rows = self._cursor.fetchall()
        has_more = len(rows) > limit
        result_rows = rows[:limit]
        items = tuple(_audit(row) for row in result_rows)
        next_cursor = encode_cursor(items[-1].occurred_at, items[-1].id) if (has_more and items) else None
        return items, next_cursor, has_more

    # ----------------------------------------------------------- retention

    def purge_expired_callback_tokens(self, now: datetime) -> int:
        self._cursor.execute(
            "DELETE FROM callback_token_uses WHERE expires_at < %s", (now,)
        )
        return self._cursor.rowcount

    def purge_completed_notifications(self, cutoff: datetime) -> int:
        self._cursor.execute(
            "DELETE FROM notifications WHERE status = 'delivered' AND delivered_at < %s", (cutoff,)
        )
        return self._cursor.rowcount

    def purge_old_pipeline_logs(self, cutoff: datetime) -> int:
        """Console lines of runs that reached a terminal state before `cutoff`.

        The run row, its digest, evidence and audit trail stay: they are what a later
        question about a release is answered from. The console output is the bulk and
        the least asked-for part; it is what a retention window is for.
        """

        self._cursor.execute(
            """
            DELETE FROM pipeline_logs WHERE pipeline_run_id IN (
                SELECT id FROM pipeline_runs
                WHERE updated_at < %s AND status IN ('succeeded', 'failed', 'cancelled', 'rolled_back')
            )
            """,
            (cutoff,),
        )
        return self._cursor.rowcount

    def purge_old_delivery_events(self, cutoff: datetime) -> int:
        self._cursor.execute(
            "DELETE FROM delivery_events WHERE occurred_at < %s", (cutoff,)
        )
        return self._cursor.rowcount

    # ----------------------------------------------------------- governance & policy

    def record_policy_decision(self, decision: PolicyDecisionRecord) -> None:
        self._cursor.execute(
            """
            INSERT INTO policy_decisions (
                id, scope, target_type, target_id, allowed, reason, risk_score,
                checks, rules_evaluated, evaluator, evaluated_at, metadata
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s::jsonb)
            """,
            (
                decision.id,
                decision.scope,
                decision.target_type,
                decision.target_id,
                decision.allowed,
                decision.reason,
                decision.risk_score,
                json.dumps(decision.checks, default=str),
                decision.rules_evaluated,
                decision.evaluator,
                decision.evaluated_at,
                json.dumps(decision.metadata, default=str),
            ),
        )

    def policy_decisions_paginated(
        self,
        scope: str | None = None,
        target_type: str | None = None,
        target_id: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> tuple[tuple[PolicyDecisionRecord, ...], str | None, bool]:
        clauses: list[str] = []
        arguments: list[Any] = []
        if scope is not None:
            clauses.append("scope = %s")
            arguments.append(scope)
        if target_type is not None:
            clauses.append("target_type = %s")
            arguments.append(target_type)
        if target_id is not None:
            clauses.append("target_id = %s")
            arguments.append(target_id)
        decoded = decode_cursor(cursor)
        if decoded is not None:
            ts, record_id = decoded
            clauses.append("(evaluated_at, id) < (%s, %s)")
            arguments.extend([ts, record_id])
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        query = f"SELECT {POLICY_DECISION_COLUMNS} FROM policy_decisions{where} ORDER BY evaluated_at DESC, id DESC LIMIT %s"
        arguments.append(limit + 1)
        self._cursor.execute(query, tuple(arguments))
        rows = self._cursor.fetchall()
        has_more = len(rows) > limit
        result_rows = rows[:limit]
        items = tuple(_policy_decision(row) for row in result_rows)
        next_cursor = encode_cursor(items[-1].evaluated_at, items[-1].id) if (has_more and items) else None
        return items, next_cursor, has_more

    def insert_security_exception(self, exception: SecurityExceptionRecord) -> None:
        self._cursor.execute(
            """
            INSERT INTO security_exceptions (
                id, cve, artifact_digest, owner, reason, approved_by, status,
                created_at, expires_at, revoked_at, revoked_by
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                exception.id,
                exception.cve,
                exception.artifact_digest,
                exception.owner,
                exception.reason,
                exception.approved_by,
                exception.status,
                exception.created_at,
                exception.expires_at,
                exception.revoked_at,
                exception.revoked_by,
            ),
        )

    def security_exceptions(
        self, active_only: bool = False, now: datetime | None = None
    ) -> tuple[SecurityExceptionRecord, ...]:
        clauses: list[str] = []
        arguments: list[Any] = []
        if active_only:
            ts = now or datetime.now(timezone.utc)
            clauses.append("status = 'active' AND expires_at > %s AND revoked_at IS NULL")
            arguments.append(ts)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        query = f"SELECT {SECURITY_EXCEPTION_COLUMNS} FROM security_exceptions{where} ORDER BY created_at DESC"
        self._cursor.execute(query, tuple(arguments))
        return tuple(_security_exception(row) for row in self._cursor.fetchall())

    def revoke_security_exception(
        self, exception_id: UUID, revoked_by: str, revoked_at: datetime
    ) -> bool:
        self._cursor.execute(
            """
            UPDATE security_exceptions
               SET status = 'revoked', revoked_by = %s, revoked_at = %s
             WHERE id = %s AND status = 'active'
            """,
            (revoked_by, revoked_at, exception_id),
        )
        return self._cursor.rowcount > 0

    def insert_security_waiver(self, waiver: SecurityWaiver) -> None:
        self._cursor.execute(
            """
            INSERT INTO security_waivers (
                id, cve_id, module_id, reason, approved_by, status, expires_at, created_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                waiver.id,
                waiver.cve_id,
                waiver.module_id,
                waiver.reason,
                waiver.approved_by,
                waiver.status.value,
                waiver.expires_at,
                waiver.created_at,
            ),
        )

    def security_waivers(
        self, module_id: str | None = None, active_only: bool = True
    ) -> tuple[SecurityWaiver, ...]:
        clauses: list[str] = []
        arguments: list[Any] = []
        if module_id is not None:
            clauses.append("(module_id IS NULL OR module_id = %s)")
            arguments.append(module_id)
        if active_only:
            clauses.append("status = 'active' AND expires_at > now()")
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        query = f"SELECT {SECURITY_WAIVER_COLUMNS} FROM security_waivers{where} ORDER BY created_at DESC"
        self._cursor.execute(query, tuple(arguments))
        return tuple(_security_waiver(row) for row in self._cursor.fetchall())

    def get_active_waiver(
        self, cve_id: str, module_id: str | None = None
    ) -> SecurityWaiver | None:
        clauses = ["cve_id = %s", "status = 'active'", "expires_at > now()"]
        arguments: list[Any] = [cve_id]
        if module_id is not None:
            clauses.append("(module_id IS NULL OR module_id = %s)")
            arguments.append(module_id)
        where = f" WHERE {' AND '.join(clauses)}"
        query = f"SELECT {SECURITY_WAIVER_COLUMNS} FROM security_waivers{where} ORDER BY created_at DESC LIMIT 1"
        self._cursor.execute(query, tuple(arguments))
        row = self._cursor.fetchone()
        return _security_waiver(row) if row else None

    def revoke_security_waiver(self, waiver_id: UUID) -> bool:
        self._cursor.execute(
            """
            UPDATE security_waivers
               SET status = 'revoked'
             WHERE id = %s AND status = 'active'
            """,
            (waiver_id,),
        )
        return self._cursor.rowcount > 0

    def upsert_server_maintenance(self, state: ServerMaintenanceState) -> None:
        self._cursor.execute(
            """
            INSERT INTO server_maintenance_states (
                server_name, in_maintenance, reason, updated_by, updated_at
            ) VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (server_name) DO UPDATE SET
                in_maintenance = EXCLUDED.in_maintenance,
                reason = EXCLUDED.reason,
                updated_by = EXCLUDED.updated_by,
                updated_at = EXCLUDED.updated_at
            """,
            (
                state.server_name,
                state.in_maintenance,
                state.reason,
                state.updated_by,
                state.updated_at,
            ),
        )

    def get_server_maintenance(self, server_name: str) -> ServerMaintenanceState | None:
        query = f"SELECT {SERVER_MAINTENANCE_COLUMNS} FROM server_maintenance_states WHERE server_name = %s"
        self._cursor.execute(query, (server_name,))
        row = self._cursor.fetchone()
        return _server_maintenance(row) if row else None

    def list_server_maintenance(self) -> tuple[ServerMaintenanceState, ...]:
        query = f"SELECT {SERVER_MAINTENANCE_COLUMNS} FROM server_maintenance_states ORDER BY server_name ASC"
        self._cursor.execute(query)
        return tuple(_server_maintenance(row) for row in self._cursor.fetchall())

    def upsert_server_telemetry(self, telemetry: ServerTelemetry) -> None:
        self._cursor.execute(
            """
            INSERT INTO server_telemetry (server_name, cpu_percent, mem_percent, disk_percent, observed_at)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (server_name) DO UPDATE SET
                cpu_percent = EXCLUDED.cpu_percent,
                mem_percent = EXCLUDED.mem_percent,
                disk_percent = EXCLUDED.disk_percent,
                observed_at = EXCLUDED.observed_at
            """,
            (telemetry.server_name, telemetry.cpu_percent, telemetry.mem_percent, telemetry.disk_percent, telemetry.observed_at),
        )

    def stage_catalog(self) -> tuple[StageDefinition, ...]:
        self._cursor.execute(f"SELECT {STAGE_CATALOG_COLUMNS} FROM stage_catalog ORDER BY position ASC, id ASC")
        return tuple(_stage_definition(row) for row in self._cursor.fetchall())

    def stage_definition(self, stage_id: str) -> StageDefinition | None:
        self._cursor.execute(f"SELECT {STAGE_CATALOG_COLUMNS} FROM stage_catalog WHERE id = %s", (stage_id,))
        row = self._cursor.fetchone()
        return _stage_definition(row) if row else None

    def upsert_stage_definition(self, stage: StageDefinition) -> None:
        self._cursor.execute(
            """
            INSERT INTO stage_catalog (id, name, category, description, kind, script, after_stage,
                                       required, enabled_by_default, position, created_by, created_at, updated_at,
                                       status, approved_by, parameters)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
            ON CONFLICT (id) DO UPDATE SET
                name = EXCLUDED.name, category = EXCLUDED.category, description = EXCLUDED.description,
                script = EXCLUDED.script, after_stage = EXCLUDED.after_stage, required = EXCLUDED.required,
                enabled_by_default = EXCLUDED.enabled_by_default, position = EXCLUDED.position,
                updated_at = EXCLUDED.updated_at, status = EXCLUDED.status, approved_by = EXCLUDED.approved_by,
                parameters = EXCLUDED.parameters
            """,
            (stage.id, stage.name, stage.category, stage.description, stage.kind, stage.script, stage.after_stage,
             stage.required, stage.enabled_by_default, stage.position, stage.created_by, stage.created_at, stage.updated_at,
             stage.status, stage.approved_by, json.dumps(list(stage.parameters))),
        )

    def delete_stage_definition(self, stage_id: str) -> bool:
        self._cursor.execute("DELETE FROM stage_catalog WHERE id = %s AND kind = 'custom'", (stage_id,))
        return self._cursor.rowcount > 0

    # ------------------------------------------------------------ agent fleet

    def upsert_agent_connection(self, connection: AgentConnection) -> None:
        self._cursor.execute(
            """
            INSERT INTO agent_connections (hostname, agent_id, replica_id, token_jti, connected_at, last_seen_at)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (hostname) DO UPDATE SET
                agent_id = EXCLUDED.agent_id, replica_id = EXCLUDED.replica_id, token_jti = EXCLUDED.token_jti,
                connected_at = EXCLUDED.connected_at, last_seen_at = EXCLUDED.last_seen_at
            """,
            (connection.hostname, connection.agent_id, connection.replica_id, connection.token_jti,
             connection.connected_at, connection.last_seen_at),
        )

    def touch_agent_connection(self, hostname: str, replica_id: str, seen_at: datetime) -> bool:
        self._cursor.execute(
            "UPDATE agent_connections SET last_seen_at = %s WHERE hostname = %s AND replica_id = %s",
            (seen_at, hostname, replica_id),
        )
        return self._cursor.rowcount > 0

    def delete_agent_connection(self, hostname: str, replica_id: str) -> bool:
        # Only the replica that holds the socket may remove the row: a late disconnect on
        # replica A must not erase the agent's fresh reconnection to replica B.
        self._cursor.execute(
            "DELETE FROM agent_connections WHERE hostname = %s AND replica_id = %s", (hostname, replica_id)
        )
        return self._cursor.rowcount > 0

    def list_agent_connections(self) -> tuple[AgentConnection, ...]:
        self._cursor.execute(
            "SELECT hostname, agent_id, replica_id, token_jti, connected_at, last_seen_at FROM agent_connections ORDER BY hostname"
        )
        return tuple(AgentConnection(**row) for row in self._cursor.fetchall())

    def insert_agent_command(self, command: AgentCommand) -> None:
        self._cursor.execute(
            """
            INSERT INTO agent_commands (id, hostname, command, requested_by, status, result, created_at, expires_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (command.id, command.hostname, command.command, command.requested_by, command.status,
             json.dumps(command.result) if command.result is not None else None, command.created_at, command.expires_at),
        )

    def claim_agent_commands(self, replica_id: str, hostnames: list[str], now: datetime) -> tuple[AgentCommand, ...]:
        """Take every pending command for these hosts, exactly once across replicas."""

        if not hostnames:
            return ()
        self._cursor.execute(
            f"""
            UPDATE agent_commands SET status = 'sent', claimed_by = %s, claimed_at = %s
            WHERE id IN (
                SELECT id FROM agent_commands
                WHERE status = 'pending' AND hostname = ANY(%s) AND expires_at > %s
                ORDER BY created_at FOR UPDATE SKIP LOCKED
            )
            RETURNING {AGENT_COMMAND_COLUMNS}
            """,
            (replica_id, now, list(hostnames), now),
        )
        return tuple(_agent_command(row) for row in self._cursor.fetchall())

    def complete_agent_command(self, command_id: UUID, status: str, result: dict[str, Any], completed_at: datetime) -> bool:
        self._cursor.execute(
            "UPDATE agent_commands SET status = %s, result = %s, completed_at = %s WHERE id = %s AND status IN ('pending', 'sent')",
            (status, json.dumps(result), completed_at, command_id),
        )
        return self._cursor.rowcount > 0

    def agent_command(self, command_id: UUID) -> AgentCommand | None:
        self._cursor.execute(f"SELECT {AGENT_COMMAND_COLUMNS} FROM agent_commands WHERE id = %s", (command_id,))
        row = self._cursor.fetchone()
        return _agent_command(row) if row else None

    def expire_agent_commands(self, now: datetime) -> int:
        self._cursor.execute(
            "UPDATE agent_commands SET status = 'expired', completed_at = %s WHERE status IN ('pending', 'sent') AND expires_at <= %s",
            (now, now),
        )
        return self._cursor.rowcount

    def try_advisory_lock(self, key: int) -> bool:
        """A transaction-scoped advisory lock: held until this session commits or rolls back.

        Background loops (reconciler, outbox) take one per pass so that with several API
        replicas exactly one runs the pass; the others skip it instead of racing.
        """

        self._cursor.execute("SELECT pg_try_advisory_xact_lock(%s) AS locked", (key,))
        row = self._cursor.fetchone()
        return bool(row and row["locked"])

    def get_server_telemetry(self, server_name: str) -> ServerTelemetry | None:
        self._cursor.execute(
            "SELECT server_name, cpu_percent, mem_percent, disk_percent, observed_at FROM server_telemetry WHERE server_name = %s",
            (server_name,),
        )
        row = self._cursor.fetchone()
        if not row:
            return None
        return ServerTelemetry(
            server_name=row["server_name"],
            cpu_percent=float(row["cpu_percent"]),
            mem_percent=float(row["mem_percent"]),
            disk_percent=float(row["disk_percent"]),
            observed_at=row["observed_at"],
        )

    def insert_break_glass_request(self, record: BreakGlassRecord) -> None:
        self._cursor.execute(
            """
            INSERT INTO break_glass_requests (
                id, target_type, target_id, requested_by, reason, incident_ticket,
                status, approved_by, created_at, approved_at, expires_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                record.id,
                record.target_type,
                record.target_id,
                record.requested_by,
                record.reason,
                record.incident_ticket,
                record.status,
                record.approved_by,
                record.created_at,
                record.approved_at,
                record.expires_at,
            ),
        )

    def break_glass_request(self, request_id: UUID) -> BreakGlassRecord | None:
        self._cursor.execute(
            f"SELECT {BREAK_GLASS_COLUMNS} FROM break_glass_requests WHERE id = %s",
            (request_id,),
        )
        row = self._cursor.fetchone()
        return _break_glass(row) if row else None

    def approve_break_glass_request(
        self, request_id: UUID, approved_by: str, approved_at: datetime, expires_at: datetime
    ) -> BreakGlassRecord | None:
        self._cursor.execute(
            f"""
            UPDATE break_glass_requests
               SET status = 'active', approved_by = %s, approved_at = %s, expires_at = %s
             WHERE id = %s AND status = 'pending'
            RETURNING {BREAK_GLASS_COLUMNS}
            """,
            (approved_by, approved_at, expires_at, request_id),
        )
        row = self._cursor.fetchone()
        return _break_glass(row) if row else None

    def active_break_glass(
        self, target_type: str, target_id: str, now: datetime
    ) -> BreakGlassRecord | None:
        self._cursor.execute(
            f"""
            SELECT {BREAK_GLASS_COLUMNS} FROM break_glass_requests
             WHERE target_type = %s AND target_id = %s AND status = 'active' AND expires_at > %s
             ORDER BY expires_at DESC LIMIT 1
            """,
            (target_type, target_id, now),
        )
        row = self._cursor.fetchone()
        return _break_glass(row) if row else None

    def get_resource_quota(self, scope: str, scope_id: str) -> ResourceQuotaRecord | None:
        self._cursor.execute(
            f"SELECT {RESOURCE_QUOTA_COLUMNS} FROM resource_quotas WHERE scope = %s AND scope_id = %s",
            (scope, scope_id),
        )
        row = self._cursor.fetchone()
        return _resource_quota(row) if row else None

    def set_resource_quota(self, record: ResourceQuotaRecord) -> None:
        self._cursor.execute(
            """
            INSERT INTO resource_quotas (
                id, scope, scope_id, max_concurrent_pipelines, max_concurrent_deployments,
                max_production_requests_per_day, created_at, updated_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (scope, scope_id) DO UPDATE SET
                max_concurrent_pipelines = EXCLUDED.max_concurrent_pipelines,
                max_concurrent_deployments = EXCLUDED.max_concurrent_deployments,
                max_production_requests_per_day = EXCLUDED.max_production_requests_per_day,
                updated_at = EXCLUDED.updated_at
            """,
            (
                record.id,
                record.scope,
                record.scope_id,
                record.max_concurrent_pipelines,
                record.max_concurrent_deployments,
                record.max_production_requests_per_day,
                record.created_at,
                record.updated_at,
            ),
        )

    # ----------------------------------------------- catalog & self-service

    def insert_catalog_service(self, service: CatalogServiceRecord) -> None:
        self._cursor.execute(
            """
            INSERT INTO catalog_services (
                id, name, description, owning_team, tier, lifecycle, repo_url, docs_url, metadata, created_at, updated_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s)
            """,
            (
                service.id,
                service.name,
                service.description,
                service.owning_team,
                service.tier,
                service.lifecycle,
                service.repo_url,
                service.docs_url,
                json.dumps(service.metadata, default=str),
                service.created_at,
                service.updated_at,
            ),
        )

    def update_catalog_service(self, service: CatalogServiceRecord) -> None:
        self._cursor.execute(
            """
            UPDATE catalog_services
               SET name = %s, description = %s, owning_team = %s, tier = %s, lifecycle = %s,
                   repo_url = %s, docs_url = %s, metadata = %s::jsonb, updated_at = %s
             WHERE id = %s
            """,
            (
                service.name,
                service.description,
                service.owning_team,
                service.tier,
                service.lifecycle,
                service.repo_url,
                service.docs_url,
                json.dumps(service.metadata, default=str),
                service.updated_at,
                service.id,
            ),
        )

    def catalog_service(self, service_id: str) -> CatalogServiceRecord | None:
        self._cursor.execute(
            f"SELECT {CATALOG_SERVICE_COLUMNS} FROM catalog_services WHERE id = %s",
            (service_id,),
        )
        row = self._cursor.fetchone()
        return _catalog_service(row) if row else None

    def list_catalog_services(
        self,
        owning_team: str | None = None,
        tier: str | None = None,
        lifecycle: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> tuple[tuple[CatalogServiceRecord, ...], str | None, bool]:
        limit = max(1, min(limit, 200))
        where_clauses: list[str] = []
        params: list[Any] = []

        if owning_team:
            where_clauses.append("owning_team = %s")
            params.append(owning_team)
        if tier:
            where_clauses.append("tier = %s")
            params.append(tier)
        if lifecycle:
            where_clauses.append("lifecycle = %s")
            params.append(lifecycle)

        decoded = decode_cursor(cursor)
        if decoded:
            c_ts, c_id = decoded
            where_clauses.append("(created_at, id) < (%s, %s)")
            params.extend([c_ts, c_id])

        where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
        query = f"""
            SELECT {CATALOG_SERVICE_COLUMNS}
              FROM catalog_services
             {where_sql}
             ORDER BY created_at DESC, id DESC
             LIMIT %s
        """
        params.append(limit + 1)
        self._cursor.execute(query, tuple(params))
        rows = self._cursor.fetchall()
        has_more = len(rows) > limit
        selected_rows = rows[:limit]
        items = tuple(_catalog_service(r) for r in selected_rows)
        next_cursor = None
        if has_more and selected_rows:
            last = selected_rows[-1]
            next_cursor = encode_cursor(last["created_at"], last["id"])
        return items, next_cursor, has_more

    def insert_service_dependency(self, dep: ServiceDependencyRecord) -> None:
        self._cursor.execute(
            """
            INSERT INTO catalog_service_dependencies (
                id, source_service_id, target_service_id, dependency_type, description, created_at
            ) VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (source_service_id, target_service_id) DO UPDATE SET
                dependency_type = EXCLUDED.dependency_type,
                description = EXCLUDED.description
            """,
            (
                dep.id,
                dep.source_service_id,
                dep.target_service_id,
                dep.dependency_type,
                dep.description,
                dep.created_at,
            ),
        )

    def delete_service_dependency(self, source_service_id: str, target_service_id: str) -> bool:
        self._cursor.execute(
            "DELETE FROM catalog_service_dependencies WHERE source_service_id = %s AND target_service_id = %s",
            (source_service_id, target_service_id),
        )
        return self._cursor.rowcount > 0

    def service_dependencies(self, service_id: str) -> tuple[ServiceDependencyRecord, ...]:
        self._cursor.execute(
            f"SELECT {SERVICE_DEPENDENCY_COLUMNS} FROM catalog_service_dependencies WHERE source_service_id = %s OR target_service_id = %s ORDER BY created_at",
            (service_id, service_id),
        )
        return tuple(_service_dependency(row) for row in self._cursor.fetchall())

    def insert_catalog_template(self, template: CatalogTemplateRecord) -> None:
        self._cursor.execute(
            """
            INSERT INTO catalog_templates (
                id, version, name, description, category, parameters_schema, pipeline_definition,
                is_deprecated, created_at, updated_at
            ) VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s, %s)
            ON CONFLICT (id, version) DO UPDATE SET
                name = EXCLUDED.name,
                description = EXCLUDED.description,
                category = EXCLUDED.category,
                parameters_schema = EXCLUDED.parameters_schema,
                pipeline_definition = EXCLUDED.pipeline_definition,
                is_deprecated = EXCLUDED.is_deprecated,
                updated_at = EXCLUDED.updated_at
            """,
            (
                template.id,
                template.version,
                template.name,
                template.description,
                template.category,
                json.dumps(template.parameters_schema, default=str),
                json.dumps(template.pipeline_definition, default=str),
                template.is_deprecated,
                template.created_at,
                template.updated_at,
            ),
        )

    def catalog_template(
        self, template_id: str, version: str | None = None
    ) -> CatalogTemplateRecord | None:
        if version:
            self._cursor.execute(
                f"SELECT {CATALOG_TEMPLATE_COLUMNS} FROM catalog_templates WHERE id = %s AND version = %s",
                (template_id, version),
            )
        else:
            self._cursor.execute(
                f"SELECT {CATALOG_TEMPLATE_COLUMNS} FROM catalog_templates WHERE id = %s ORDER BY created_at DESC LIMIT 1",
                (template_id,),
            )
        row = self._cursor.fetchone()
        return _catalog_template(row) if row else None

    def list_catalog_templates(
        self, category: str | None = None, include_deprecated: bool = False
    ) -> tuple[CatalogTemplateRecord, ...]:
        where_clauses: list[str] = []
        params: list[Any] = []
        if category:
            where_clauses.append("category = %s")
            params.append(category)
        if not include_deprecated:
            where_clauses.append("is_deprecated = FALSE")

        where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
        self._cursor.execute(
            f"SELECT {CATALOG_TEMPLATE_COLUMNS} FROM catalog_templates {where_sql} ORDER BY id, created_at DESC",
            tuple(params),
        )
        return tuple(_catalog_template(row) for row in self._cursor.fetchall())

    def insert_preview_environment(self, preview: PreviewEnvironmentRecord) -> None:
        self._cursor.execute(
            """
            INSERT INTO preview_environments (
                id, application_id, pull_request_id, commit_sha, namespace, url, status,
                ttl_seconds, expires_at, created_by, created_at, destroyed_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                preview.id,
                preview.application_id,
                preview.pull_request_id,
                preview.commit_sha,
                preview.namespace,
                preview.url,
                preview.status,
                preview.ttl_seconds,
                preview.expires_at,
                preview.created_by,
                preview.created_at,
                preview.destroyed_at,
            ),
        )

    def update_preview_environment_status(
        self, preview_id: str, status: str, destroyed_at: datetime | None = None
    ) -> PreviewEnvironmentRecord | None:
        self._cursor.execute(
            f"""
            UPDATE preview_environments
               SET status = %s, destroyed_at = COALESCE(%s, destroyed_at)
             WHERE id = %s
            RETURNING {PREVIEW_ENVIRONMENT_COLUMNS}
            """,
            (status, destroyed_at, preview_id),
        )
        row = self._cursor.fetchone()
        return _preview_environment(row) if row else None

    def preview_environment(self, preview_id: str) -> PreviewEnvironmentRecord | None:
        self._cursor.execute(
            f"SELECT {PREVIEW_ENVIRONMENT_COLUMNS} FROM preview_environments WHERE id = %s",
            (preview_id,),
        )
        row = self._cursor.fetchone()
        return _preview_environment(row) if row else None

    def list_preview_environments(
        self, application_id: UUID | None = None, status: str | None = None
    ) -> tuple[PreviewEnvironmentRecord, ...]:
        where_clauses: list[str] = []
        params: list[Any] = []
        if application_id:
            where_clauses.append("application_id = %s")
            params.append(application_id)
        if status:
            where_clauses.append("status = %s")
            params.append(status)

        where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
        self._cursor.execute(
            f"SELECT {PREVIEW_ENVIRONMENT_COLUMNS} FROM preview_environments {where_sql} ORDER BY created_at DESC",
            tuple(params),
        )
        return tuple(_preview_environment(row) for row in self._cursor.fetchall())

    def expired_preview_environments(self, now: datetime) -> tuple[PreviewEnvironmentRecord, ...]:
        self._cursor.execute(
            f"SELECT {PREVIEW_ENVIRONMENT_COLUMNS} FROM preview_environments WHERE status = 'active' AND expires_at <= %s",
            (now,),
        )
        return tuple(_preview_environment(row) for row in self._cursor.fetchall())

    def insert_resource_request(self, request: ResourceRequestRecord) -> None:
        self._cursor.execute(
            """
            INSERT INTO resource_requests (
                id, application_id, team_id, environment, resource_type, spec, status,
                status_reason, provider, outputs, requested_by, approved_by, created_at, updated_at
            ) VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s::jsonb, %s, %s, %s, %s)
            """,
            (
                request.id,
                request.application_id,
                request.team_id,
                request.environment,
                request.resource_type,
                json.dumps(request.spec, default=str),
                request.status,
                request.status_reason,
                request.provider,
                json.dumps(request.outputs, default=str),
                request.requested_by,
                request.approved_by,
                request.created_at,
                request.updated_at,
            ),
        )

    def update_resource_request(
        self,
        request_id: UUID,
        *,
        status: str,
        status_reason: str | None = None,
        provider: str | None = None,
        outputs: dict[str, Any] | None = None,
        approved_by: str | None = None,
    ) -> ResourceRequestRecord | None:
        updates = ["status = %s", "updated_at = NOW()"]
        params: list[Any] = [status]
        if status_reason is not None:
            updates.append("status_reason = %s")
            params.append(status_reason)
        if provider is not None:
            updates.append("provider = %s")
            params.append(provider)
        if outputs is not None:
            updates.append("outputs = %s::jsonb")
            params.append(json.dumps(outputs, default=str))
        if approved_by is not None:
            updates.append("approved_by = %s")
            params.append(approved_by)
        params.append(request_id)

        self._cursor.execute(
            f"""
            UPDATE resource_requests
               SET {', '.join(updates)}
             WHERE id = %s
            RETURNING {RESOURCE_REQUEST_COLUMNS}
            """,
            tuple(params),
        )
        row = self._cursor.fetchone()
        return _resource_request(row) if row else None

    def resource_request(self, request_id: UUID) -> ResourceRequestRecord | None:
        self._cursor.execute(
            f"SELECT {RESOURCE_REQUEST_COLUMNS} FROM resource_requests WHERE id = %s",
            (request_id,),
        )
        row = self._cursor.fetchone()
        return _resource_request(row) if row else None

    def list_resource_requests(
        self,
        application_id: UUID | None = None,
        team_id: str | None = None,
        environment: str | None = None,
        status: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> tuple[tuple[ResourceRequestRecord, ...], str | None, bool]:
        limit = max(1, min(limit, 200))
        where_clauses: list[str] = []
        params: list[Any] = []

        if application_id:
            where_clauses.append("application_id = %s")
            params.append(application_id)
        if team_id:
            where_clauses.append("team_id = %s")
            params.append(team_id)
        if environment:
            where_clauses.append("environment = %s")
            params.append(environment)
        if status:
            where_clauses.append("status = %s")
            params.append(status)

        decoded = decode_cursor(cursor)
        if decoded:
            c_ts, c_id = decoded
            where_clauses.append("(created_at, id) < (%s, %s)")
            params.extend([c_ts, c_id])

        where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
        query = f"""
            SELECT {RESOURCE_REQUEST_COLUMNS}
              FROM resource_requests
             {where_sql}
             ORDER BY created_at DESC, id DESC
             LIMIT %s
        """
        params.append(limit + 1)
        self._cursor.execute(query, tuple(params))
        rows = self._cursor.fetchall()
        has_more = len(rows) > limit
        selected_rows = rows[:limit]
        items = tuple(_resource_request(r) for r in selected_rows)
        next_cursor = None
        if has_more and selected_rows:
            last = selected_rows[-1]
            next_cursor = encode_cursor(last["created_at"], last["id"])
        return items, next_cursor, has_more



class PostgresConnectionPool:
    """Thread-safe connection pool with checkout timeout, liveness checks, and clean shutdown."""

    def __init__(self, url: str, min_size: int = 2, max_size: int = 20, timeout: float = 10.0) -> None:
        self.url = url
        self.min_size = min_size
        self.max_size = max_size
        self.timeout = timeout
        self._pool: queue.Queue[tuple[psycopg.Connection, float]] = queue.Queue(maxsize=max_size)
        self._created_count = 0
        self._lock = threading.Lock()
        self._closed = False

    def get(self) -> psycopg.Connection:
        if self._closed:
            raise RuntimeError("PostgresConnectionPool is closed")

        with self._lock:
            if self._created_count < self.max_size and self._pool.empty():
                conn = psycopg.connect(self.url, row_factory=dict_row)
                self._created_count += 1
                return conn

        try:
            conn, checkout_time = self._pool.get(timeout=self.timeout)
        except queue.Empty:
            with self._lock:
                if self._created_count < self.max_size:
                    conn = psycopg.connect(self.url, row_factory=dict_row)
                    self._created_count += 1
                    return conn
            raise TimeoutError(f"Database connection pool exhausted (max={self.max_size})")

        try:
            if conn.closed:
                conn = psycopg.connect(self.url, row_factory=dict_row)
            elif datetime.now(timezone.utc).timestamp() - checkout_time > 30.0:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1")
        except Exception:
            try:
                conn.close()
            except Exception:
                pass
            conn = psycopg.connect(self.url, row_factory=dict_row)

        return conn

    def put(self, conn: psycopg.Connection) -> None:
        if self._closed or conn.closed:
            try:
                conn.close()
            except Exception:
                pass
            with self._lock:
                self._created_count = max(0, self._created_count - 1)
            return

        try:
            if not conn.closed:
                conn.rollback()
            self._pool.put_nowait((conn, datetime.now(timezone.utc).timestamp()))
        except queue.Full:
            try:
                conn.close()
            except Exception:
                pass
            with self._lock:
                self._created_count = max(0, self._created_count - 1)

    def stats(self) -> dict[str, int]:
        idle = self._pool.qsize()
        with self._lock:
            total = self._created_count
        return {"idle": idle, "active": max(0, total - idle), "total": total, "max": self.max_size}

    def close(self) -> None:
        self._closed = True
        while not self._pool.empty():
            try:
                conn, _ = self._pool.get_nowait()
                conn.close()
            except Exception:
                pass
        with self._lock:
            self._created_count = 0


class PostgresDatabase:
    """Connection-pooled PostgreSQL database with transactional sessions."""

    def __init__(
        self,
        url: str,
        min_pool_size: int | None = None,
        max_pool_size: int | None = None,
        pool_timeout: float | None = None,
    ) -> None:
        self.url = url
        self.last_error: str | None = None
        min_size = min_pool_size or int(os.environ.get("NETCI_DB_POOL_MIN_SIZE", "5"))
        max_size = max_pool_size or int(os.environ.get("NETCI_DB_POOL_MAX_SIZE", "30"))
        timeout = pool_timeout or float(os.environ.get("NETCI_DB_POOL_TIMEOUT", "10.0"))
        self._pool = (
            PostgresConnectionPool(url, min_size=min_size, max_size=max_size, timeout=timeout)
            if psycopg
            else None
        )

    def describe(self) -> str:
        return "postgresql"

    def _connect(self):
        if psycopg is None:
            raise RuntimeError("psycopg is required when DATABASE_URL is configured")
        return psycopg.connect(self.url, row_factory=dict_row)

    def pool_stats(self) -> dict[str, int]:
        if self._pool:
            return self._pool.stats()
        return {"idle": 0, "active": 0, "total": 0, "max": 0}

    def close(self) -> None:
        if self._pool:
            self._pool.close()

    @contextmanager
    def transaction(self):
        """Yield one transaction: commit on a clean exit, roll back on any exception.

        A unique-constraint violation is reported as `ConcurrentModification` rather than
        as a storage failure. On these tables it means exactly one thing -- another
        transaction inserted the same key between this one's check and its write -- and
        the caller owes the client a 409, not a 503 pointing at the database.
        """
        connection = self._pool.get() if self._pool else self._connect()
        try:
            with connection.cursor() as cursor:
                try:
                    yield PostgresSession(cursor)
                except psycopg.errors.UniqueViolation as exc:
                    connection.rollback()
                    raise ConcurrentModification(
                        f"a competing transaction already wrote this record: {exc.diag.constraint_name}"
                    ) from exc
                except BaseException:
                    connection.rollback()
                    raise
                try:
                    connection.commit()
                except psycopg.errors.UniqueViolation as exc:
                    connection.rollback()
                    raise ConcurrentModification(
                        f"a competing transaction already wrote this record: {exc.diag.constraint_name}"
                    ) from exc
        finally:
            if self._pool:
                self._pool.put(connection)
            else:
                connection.close()

    def health(self) -> str:
        try:
            if self._pool:
                conn = self._pool.get()
                try:
                    with conn.cursor() as cursor:
                        cursor.execute("SELECT 1")
                finally:
                    self._pool.put(conn)
            else:
                with self._connect() as connection:
                    with connection.cursor() as cursor:
                        cursor.execute("SELECT 1")
            self.last_error = None
            return "ok"
        except Exception as exc:
            self.last_error = str(exc)
            return f"unavailable: {exc}"

