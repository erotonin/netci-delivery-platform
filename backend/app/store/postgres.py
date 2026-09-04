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
    ScmCommitStatus,
    ScmIntegration,
    ScmProviderType,
    ScmWebhookDelivery,
    ServerHealthRecord,
)
from ..persistence import (
    AuditRecord,
    ConcurrentModification,
    IdempotencyRow,
    StillReferenced,
    UnitOfWork,
    VersionConflict,
)
from .records import (
    DeploymentLease,
    ModuleRow,
    RequestModuleRow,
    RequestRow,
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
    " stages, owner_team, created_at"
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
    " release_plan, strategy, strategy_config"
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


def encode_cursor(timestamp: datetime, record_id: UUID | str) -> str:
    payload = f"{timestamp.isoformat()}|{record_id}"
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii")


def decode_cursor(cursor_str: str | None) -> tuple[datetime, str] | None:
    if not cursor_str:
        return None
    try:
        raw = base64.urlsafe_b64decode(cursor_str.encode("ascii")).decode("utf-8")
        ts_str, id_str = raw.split("|", 1)
        return datetime.fromisoformat(ts_str), id_str
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



def _application(row: dict[str, Any]) -> Application:
    return Application(
        name=row["name"],
        repository_url=row["repository_url"],
        pipeline_template=row["pipeline_template"],
        runtime=Runtime(row["runtime"]),
        default_environment=Environment(row["default_environment"]),
        stages=tuple(row["stages"] or []),
        owner_team=row["owner_team"],
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
                                          default_environment, stages, owner_team, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s)
                ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name, stages = EXCLUDED.stages,
                                               owner_team = EXCLUDED.owner_team
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
            f"""
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
    ) -> ModuleConfigRevision | None:
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

    def purge_old_delivery_events(self, cutoff: datetime) -> int:
        self._cursor.execute(
            "DELETE FROM delivery_events WHERE occurred_at < %s", (cutoff,)
        )
        return self._cursor.rowcount


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

    def __init__(self, url: str, max_pool_size: int = 15) -> None:
        self.url = url
        self.last_error: str | None = None
        self._pool = PostgresConnectionPool(url, max_size=max_pool_size) if psycopg else None

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

