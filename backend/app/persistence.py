"""Optional PostgreSQL persistence for the Release Portal projection.

The delivery rules remain in the domain service. This repository owns only
Portal hierarchy/read-model metadata and can fall back to the local projection
when DATABASE_URL is absent, which keeps unit tests deterministic.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from .domain.models import (
    Application,
    Deployment,
    DeploymentStatus,
    DeliveryEvent,
    DeliveryEventType,
    Environment,
    PipelineRun,
    PipelineStatus,
    Runtime,
)

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:  # pragma: no cover - optional dependency for source-only tests
    psycopg = None
    dict_row = None


logger = logging.getLogger(__name__)


def database_url() -> str:
    """Resolve the connection string, preferring a mounted secret file.

    Keeping the credential out of the process environment is the first step of the
    P1 secret-handling item; a compose/Kubernetes secret can be mounted instead.
    """

    path = os.getenv("DATABASE_URL_FILE", "").strip()
    if path:
        try:
            with open(path, encoding="utf-8") as handle:
                return handle.read().strip()
        except OSError as exc:
            logger.error("DATABASE_URL_FILE is unreadable: %s", exc)
            return ""
    return os.getenv("DATABASE_URL", "").strip()


@dataclass(frozen=True)
class AuditRecord:
    event_type: str
    application_id: UUID | None = None
    pipeline_run_id: UUID | None = None
    deployment_id: UUID | None = None
    actor: str | None = None
    correlation_id: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class IdempotencyRow:
    scope: str
    idempotency_key: str
    request_hash: str
    resource_type: str
    resource_id: UUID
    response_status: int


@dataclass
class UnitOfWork:
    """One delivery state change plus every fact it must publish atomically.

    Writing state, outbox events, audit and logs through a single transaction is
    what stops "state changed but the DORA event was lost" from being possible.
    Each run/deployment carries the version it was read at, so two concurrent
    callbacks cannot both win.
    """

    applications: list[Application] = field(default_factory=list)
    runs: list[tuple[PipelineRun, int | None]] = field(default_factory=list)
    deployments: list[tuple[Deployment, int | None]] = field(default_factory=list)
    events: list[DeliveryEvent] = field(default_factory=list)
    audit: list[AuditRecord] = field(default_factory=list)
    logs: list[tuple[UUID, list[str]]] = field(default_factory=list)
    idempotency: list[IdempotencyRow] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not any(
            (self.applications, self.runs, self.deployments, self.events, self.audit, self.logs, self.idempotency)
        )


class ConcurrentModification(RuntimeError):
    """Raised when a record changed between read and write."""


class PostgresDeliveryStore:
    def __init__(self, url: str) -> None:
        self.url = url
        self.last_error: str | None = None

    @classmethod
    def from_env(cls) -> "PostgresDeliveryStore | None":
        url = database_url()
        if not url or psycopg is None:
            return None
        return cls(url)

    def _connect(self):
        if psycopg is None:
            raise RuntimeError("psycopg is not installed")
        return psycopg.connect(self.url, row_factory=dict_row)

    def health(self) -> str:
        try:
            with self._connect() as connection:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT 1")
            self.last_error = None
            return "ok"
        except Exception as exc:
            self.last_error = str(exc)
            return f"unavailable: {exc}"

    def load(self) -> dict[str, list[Any]] | None:
        try:
            with self._connect() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT id, name, repository_url, pipeline_template, runtime, default_environment, stages, created_at"
                        " FROM applications ORDER BY created_at"
                    )
                    applications = [
                        Application(
                            name=row["name"],
                            repository_url=row["repository_url"],
                            pipeline_template=row["pipeline_template"],
                            runtime=Runtime(row["runtime"]),
                            default_environment=Environment(row["default_environment"]),
                            stages=tuple(row["stages"] or []),
                            id=row["id"],
                            created_at=row["created_at"],
                        )
                        for row in cursor.fetchall()
                    ]
                    cursor.execute(
                        "SELECT id, application_id, status, commit_sha, branch, environment, parameters, correlation_id,"
                        " jenkins_run_id, workflow_id, artifact_digest, started_by, version, created_at, updated_at"
                        " FROM pipeline_runs ORDER BY created_at"
                    )
                    runs = [
                        PipelineRun(
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
                            version=int(row["version"] or 1),
                            created_at=row["created_at"],
                            updated_at=row["updated_at"],
                        )
                        for row in cursor.fetchall()
                    ]
                    cursor.execute(
                        "SELECT id, application_id, pipeline_run_id, runtime, environment, status, artifact_digest,"
                        " previous_artifact_digest, approved_by, version, created_at, updated_at"
                        " FROM deployments ORDER BY created_at"
                    )
                    deployments = [
                        Deployment(
                            id=row["id"],
                            application_id=row["application_id"],
                            pipeline_run_id=row["pipeline_run_id"],
                            runtime=Runtime(row["runtime"]),
                            environment=Environment(row["environment"]),
                            status=DeploymentStatus(row["status"]),
                            artifact_digest=row["artifact_digest"],
                            previous_artifact_digest=row["previous_artifact_digest"],
                            approved_by=row["approved_by"],
                            version=int(row["version"] or 1),
                            created_at=row["created_at"],
                            updated_at=row["updated_at"],
                        )
                        for row in cursor.fetchall()
                    ]
                    cursor.execute(
                        "SELECT id, event_type, application_id, pipeline_run_id, deployment_id, commit_sha, environment,"
                        " successful, requires_intervention, occurred_at FROM delivery_events ORDER BY occurred_at, id"
                    )
                    events = [
                        DeliveryEvent(
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
                        for row in cursor.fetchall()
                    ]
                    cursor.execute(
                        "SELECT pipeline_run_id, line FROM pipeline_logs ORDER BY pipeline_run_id, sequence"
                    )
                    logs: dict[UUID, list[str]] = {}
                    for row in cursor.fetchall():
                        logs.setdefault(row["pipeline_run_id"], []).append(row["line"])
                    cursor.execute(
                        "SELECT scope, idempotency_key, request_hash, resource_type, resource_id, response_status"
                        " FROM idempotency_records"
                    )
                    idempotency = [
                        IdempotencyRow(
                            scope=row["scope"],
                            idempotency_key=row["idempotency_key"],
                            request_hash=row["request_hash"],
                            resource_type=row["resource_type"],
                            resource_id=row["resource_id"],
                            response_status=int(row["response_status"]),
                        )
                        for row in cursor.fetchall()
                    ]
            self.last_error = None
            return {
                "applications": applications,
                "runs": runs,
                "deployments": deployments,
                "events": events,
                "logs": logs,
                "idempotency": idempotency,
            }
        except Exception as exc:
            self.last_error = str(exc)
            logger.exception("delivery persistence load failed")
            return None

    def commit(self, unit: UnitOfWork) -> None:
        """Apply one unit of work in a single transaction, or raise and change nothing."""

        if unit.is_empty():
            return
        with self._connect() as connection:
            with connection.cursor() as cursor:
                for application in unit.applications:
                    cursor.execute(
                        """
                        INSERT INTO applications (id, name, repository_url, pipeline_template, runtime, default_environment, stages, created_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s)
                        ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name, stages = EXCLUDED.stages
                        """,
                        (
                            application.id,
                            application.name,
                            application.repository_url,
                            application.pipeline_template,
                            application.runtime.value,
                            application.default_environment.value,
                            json.dumps(list(application.stages)),
                            application.created_at,
                        ),
                    )
                for run, expected_version in unit.runs:
                    self._write_run(cursor, run, expected_version)
                for deployment, expected_version in unit.deployments:
                    self._write_deployment(cursor, deployment, expected_version)
                for run_id, lines in unit.logs:
                    for line in lines:
                        cursor.execute(
                            "INSERT INTO pipeline_logs (pipeline_run_id, sequence, line)"
                            " VALUES (%s, (SELECT coalesce(max(sequence), 0) + 1 FROM pipeline_logs WHERE pipeline_run_id = %s), %s)",
                            (run_id, run_id, line[:8000]),
                        )
                for event in unit.events:
                    cursor.execute(
                        """
                        INSERT INTO delivery_events (id, event_type, application_id, pipeline_run_id, deployment_id,
                                                     commit_sha, environment, successful, requires_intervention, occurred_at)
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
                        "INSERT INTO audit_events (event_type, application_id, pipeline_run_id, deployment_id, actor, correlation_id, payload)"
                        " VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb)",
                        (
                            record.event_type,
                            record.application_id,
                            record.pipeline_run_id,
                            record.deployment_id,
                            record.actor,
                            record.correlation_id,
                            json.dumps(record.payload, default=str),
                        ),
                    )
                for row in unit.idempotency:
                    cursor.execute(
                        """
                        INSERT INTO idempotency_records (scope, idempotency_key, request_hash, resource_type, resource_id, response_status, response_body)
                        VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb)
                        ON CONFLICT (scope, idempotency_key) DO NOTHING
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

    @staticmethod
    def _write_run(cursor, run: PipelineRun, expected_version: int | None) -> None:
        if expected_version is None:
            cursor.execute(
                """
                INSERT INTO pipeline_runs (id, application_id, commit_sha, branch, environment, parameters, status,
                                           jenkins_run_id, workflow_id, artifact_digest, correlation_id, started_by,
                                           version, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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
                   artifact_digest = %s, version = %s, updated_at = %s
             WHERE id = %s AND version = %s
            """,
            (
                run.status.value,
                json.dumps(run.parameters, default=str),
                run.jenkins_run_id,
                run.workflow_id,
                run.artifact_digest,
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
                INSERT INTO deployments (id, application_id, pipeline_run_id, runtime, environment, status,
                                         artifact_digest, previous_artifact_digest, approved_by, version, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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
                   approved_by = %s, version = %s, updated_at = %s
             WHERE id = %s AND version = %s
            """,
            (
                deployment.status.value,
                deployment.artifact_digest,
                deployment.previous_artifact_digest,
                deployment.approved_by,
                deployment.version,
                deployment.updated_at,
                deployment.id,
                expected_version,
            ),
        )
        if cursor.rowcount != 1:
            raise ConcurrentModification(f"deployment {deployment.id} changed since it was read")


class PostgresPortalStore:
    def __init__(self, url: str) -> None:
        self.url = url
        self.last_error: str | None = None

    @classmethod
    def from_env(cls) -> "PostgresPortalStore | None":
        url = database_url()
        if not url or psycopg is None:
            return None
        return cls(url)

    def _connect(self):
        if psycopg is None:
            raise RuntimeError("psycopg is not installed")
        return psycopg.connect(self.url, row_factory=dict_row)

    def bootstrap(self) -> bool:
        try:
            with self._connect() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        INSERT INTO systems (id, unit, description, owner, status)
                        VALUES
                          ('hello-container', 'Local Infrastructure', 'Local container delivery application', 'Admin', 'healthy'),
                          ('hello-kubernetes', 'Local Infrastructure', 'Local Kubernetes deployment application', 'Admin', 'healthy'),
                          ('hello-systemd-go', 'Local Infrastructure', 'Local systemd service application', 'Admin', 'healthy')
                        ON CONFLICT (id) DO NOTHING
                        """
                    )
                    cursor.execute(
                        """
                        INSERT INTO modules (id, system_id, runtime, name, module_type, description)
                        VALUES
                          ('hello-container', 'hello-container', 'docker', 'Hello Container', 'Backend', 'Local container delivery application'),
                          ('hello-kubernetes', 'hello-kubernetes', 'kubernetes', 'Hello Kubernetes', 'Workload', 'Local Kubernetes deployment application'),
                          ('hello-systemd-go', 'hello-systemd-go', 'systemd', 'Hello Systemd Go', 'Backend', 'Local systemd service application')
                        ON CONFLICT (id) DO NOTHING
                        """
                    )
            self.last_error = None
            return True
        except Exception as exc:
            self.last_error = str(exc)
            logger.exception("portal persistence bootstrap failed")
            return False

    def load(self) -> dict[str, list[dict[str, Any]]] | None:
        try:
            with self._connect() as connection:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT id, unit, description, owner, status FROM systems ORDER BY created_at, id")
                    systems = list(cursor.fetchall())
                    cursor.execute("SELECT id, system_id, application_id, runtime, name, module_type, description, deployment_config, pipeline_config FROM modules ORDER BY created_at, id")
                    modules = list(cursor.fetchall())
                    cursor.execute("SELECT module_id, version, metadata FROM release_versions ORDER BY created_at DESC, version DESC")
                    versions = list(cursor.fetchall())
                    cursor.execute("SELECT id, module_id, version, requested_by, scheduled_for, rollback_strategy, run_automation_tests, status, deployment_id, comment FROM production_requests ORDER BY created_at, id")
                    requests = list(cursor.fetchall())
                    cursor.execute("SELECT request_id, module_id, version, deployment_order FROM production_request_modules ORDER BY request_id, deployment_order, module_id")
                    request_modules = list(cursor.fetchall())
            self.last_error = None
            return {"systems": systems, "modules": modules, "versions": versions, "requests": requests, "request_modules": request_modules}
        except Exception as exc:
            self.last_error = str(exc)
            logger.exception("portal persistence load failed")
            return None

    def insert_system(self, record: dict[str, Any]) -> None:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO systems (id, unit, description, owner, status) VALUES (%(id)s, %(unit)s, %(description)s, %(owner)s, %(status)s)",
                    record,
                )

    def insert_module(self, record: dict[str, Any]) -> None:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO modules (id, system_id, application_id, runtime, name, module_type, description, deployment_config, pipeline_config)
                    VALUES (%(id)s, %(system_id)s, %(application_id)s, %(runtime)s, %(name)s, %(module_type)s, %(description)s, %(deployment_config)s::jsonb, %(pipeline_config)s::jsonb)
                    """,
                    {**record, "deployment_config": json.dumps(record["deployment_config"]), "pipeline_config": json.dumps(record["pipeline_config"])},
                )

    def upsert_version(self, module_id: str, version: str, metadata: dict[str, Any]) -> None:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO release_versions (module_id, version, metadata)
                    VALUES (%s, %s, %s::jsonb)
                    ON CONFLICT (module_id, version) DO UPDATE SET metadata = EXCLUDED.metadata
                    """,
                    (module_id, version, json.dumps(metadata)),
                )

    def insert_request(self, record: dict[str, Any], modules: list[dict[str, Any]]) -> None:
        primary = modules[0]
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO production_requests (
                        id, module_id, version, requested_by, scheduled_for,
                        rollback_strategy, run_automation_tests, status
                    ) VALUES (
                        %(id)s, %(module_id)s, %(version)s, %(requested_by)s, %(scheduled_for)s,
                        %(rollback_strategy)s, %(run_automation_tests)s, %(status)s
                    )
                    """,
                    {**record, "id": UUID(str(record["id"])), "module_id": primary["module_id"], "version": primary["version"]},
                )
                for module in modules:
                    cursor.execute(
                        """
                        INSERT INTO production_request_modules (request_id, module_id, version, deployment_order)
                        VALUES (%(request_id)s, %(module_id)s, %(version)s, %(deployment_order)s)
                        """,
                        {**module, "request_id": UUID(str(module["request_id"]))},
                    )

    def update_request(self, request_id: str, status: str, comment: str | None) -> None:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE production_requests SET status = %s, comment = %s WHERE id = %s",
                    (status, comment, UUID(request_id)),
                )

    def delete_system(self, system_id: str) -> None:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute("DELETE FROM modules WHERE system_id = %s", (system_id,))
                cursor.execute("DELETE FROM systems WHERE id = %s", (system_id,))

    def delete_module(self, module_id: str) -> None:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute("DELETE FROM modules WHERE id = %s", (module_id,))

