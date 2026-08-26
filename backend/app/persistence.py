"""Optional PostgreSQL persistence for the Release Portal projection.

The delivery rules remain in the domain service. This repository owns only
Portal hierarchy/read-model metadata and can fall back to the local projection
when DATABASE_URL is absent, which keeps unit tests deterministic.
"""

from __future__ import annotations

import json
import os
from typing import Any
from uuid import UUID

from .domain.models import Application, Deployment, Environment, PipelineRun, PipelineStatus, Runtime, DeploymentStatus

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:  # pragma: no cover - optional dependency for source-only tests
    psycopg = None
    dict_row = None


class PostgresDeliveryStore:
    def __init__(self, url: str) -> None:
        self.url = url

    @classmethod
    def from_env(cls) -> "PostgresDeliveryStore | None":
        url = os.getenv("DATABASE_URL", "").strip()
        if not url or psycopg is None:
            return None
        return cls(url)

    def _connect(self):
        if psycopg is None:
            raise RuntimeError("psycopg is not installed")
        return psycopg.connect(self.url, row_factory=dict_row)

    def load(self) -> dict[str, list[Any]] | None:
        try:
            with self._connect() as connection:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT id, name, repository_url, pipeline_template, runtime, default_environment, stages, created_at FROM applications ORDER BY created_at")
                    applications = [Application(name=row["name"], repository_url=row["repository_url"], pipeline_template=row["pipeline_template"], runtime=Runtime(row["runtime"]), default_environment=Environment(row["default_environment"]), stages=tuple(row["stages"] or []), id=row["id"], created_at=row["created_at"]) for row in cursor.fetchall()]
                    cursor.execute("SELECT id, application_id, status, commit_sha, branch, environment, correlation_id, jenkins_run_id, workflow_id, artifact_digest, created_at, updated_at FROM pipeline_runs ORDER BY created_at")
                    runs = [PipelineRun(id=row["id"], application_id=row["application_id"], status=PipelineStatus(row["status"]), commit_sha=row["commit_sha"], branch=row["branch"], environment=Environment(row["environment"]), correlation_id=row["correlation_id"] or "", jenkins_run_id=row["jenkins_run_id"], workflow_id=row["workflow_id"], artifact_digest=row["artifact_digest"], created_at=row["created_at"], updated_at=row["updated_at"]) for row in cursor.fetchall()]
                    cursor.execute("SELECT id, application_id, pipeline_run_id, runtime, environment, status, artifact_digest, previous_artifact_digest, approved_by, created_at, updated_at FROM deployments ORDER BY created_at")
                    deployments = [Deployment(id=row["id"], application_id=row["application_id"], pipeline_run_id=row["pipeline_run_id"], runtime=Runtime(row["runtime"]), environment=Environment(row["environment"]), status=DeploymentStatus(row["status"]), artifact_digest=row["artifact_digest"], previous_artifact_digest=row["previous_artifact_digest"], approved_by=row["approved_by"], created_at=row["created_at"], updated_at=row["updated_at"]) for row in cursor.fetchall()]
            return {"applications": applications, "runs": runs, "deployments": deployments}
        except Exception:
            return None

    def save_application(self, item: Application) -> None:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute("""
                    INSERT INTO applications (id, name, repository_url, pipeline_template, runtime, default_environment, stages, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s)
                    ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name, stages = EXCLUDED.stages
                """, (item.id, item.name, item.repository_url, item.pipeline_template, item.runtime.value, item.default_environment.value, json.dumps(list(item.stages)), item.created_at))

    def save_pipeline_run(self, item: PipelineRun) -> None:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute("""
                    INSERT INTO pipeline_runs (id, application_id, commit_sha, branch, environment, status, jenkins_run_id, workflow_id, artifact_digest, correlation_id, created_at, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (id) DO UPDATE SET status = EXCLUDED.status, jenkins_run_id = EXCLUDED.jenkins_run_id, workflow_id = EXCLUDED.workflow_id, artifact_digest = EXCLUDED.artifact_digest, updated_at = EXCLUDED.updated_at
                """, (item.id, item.application_id, item.commit_sha, item.branch, item.environment.value, item.status.value, item.jenkins_run_id, item.workflow_id, item.artifact_digest, item.correlation_id, item.created_at, item.updated_at))

    def save_deployment(self, item: Deployment) -> None:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute("""
                    INSERT INTO deployments (id, application_id, pipeline_run_id, runtime, environment, status, artifact_digest, previous_artifact_digest, approved_by, created_at, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (id) DO UPDATE SET status = EXCLUDED.status, artifact_digest = EXCLUDED.artifact_digest, previous_artifact_digest = EXCLUDED.previous_artifact_digest, approved_by = EXCLUDED.approved_by, updated_at = EXCLUDED.updated_at
                """, (item.id, item.application_id, item.pipeline_run_id, item.runtime.value, item.environment.value, item.status.value, item.artifact_digest, item.previous_artifact_digest, item.approved_by, item.created_at, item.updated_at))

    def save_audit_event(self, event_type: str, item: Application | PipelineRun | Deployment, actor: str | None = None, correlation_id: str | None = None) -> None:
        application_id = item.id if isinstance(item, Application) else item.application_id
        pipeline_run_id = item.id if isinstance(item, PipelineRun) else None
        deployment_id = item.id if isinstance(item, Deployment) else None
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute("INSERT INTO audit_events (event_type, application_id, pipeline_run_id, deployment_id, actor, correlation_id) VALUES (%s, %s, %s, %s, %s, %s)", (event_type, application_id, pipeline_run_id, deployment_id, actor, correlation_id))


class PostgresPortalStore:
    def __init__(self, url: str) -> None:
        self.url = url

    @classmethod
    def from_env(cls) -> "PostgresPortalStore | None":
        url = os.getenv("DATABASE_URL", "").strip()
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
                          ('netChat', 'Trung tâm nền tảng Công nghệ và Chuyển đổi số', 'Real-time messaging platform for internal team communication.', 'Admin', 'healthy'),
                          ('PCTT', 'Trung tâm Chăm sóc khách hàng', 'Ticketing & customer-support case tracking module.', 'Admin', 'degraded'),
                          ('NocPro5', 'Trung tâm Vận hành khai thác mạng', 'Network operations alarm monitoring & correlation.', 'Admin', 'critical')
                        ON CONFLICT (id) DO NOTHING
                        """
                    )
                    cursor.execute(
                        """
                        INSERT INTO modules (id, system_id, runtime, name, module_type, description)
                        VALUES
                          ('backend-api', 'netChat', 'docker', 'Backend API', 'Backend', 'Node.js REST & WebSocket API for auth, messaging, presence.'),
                          ('web-client', 'netChat', 'kubernetes', 'Web Client', 'Frontend', 'React SPA for desktop & mobile web messaging.'),
                          ('pctt-api', 'PCTT', 'docker', 'PCTT API', 'Backend', 'Customer support API and ticketing workflow.'),
                          ('pctt-web', 'PCTT', 'kubernetes', 'PCTT Web', 'Frontend', 'Customer support operations web application.'),
                          ('alert-correlator', 'NocPro5', 'systemd', 'Alert Correlator', 'Backend', 'Network alarm correlation and notification service.')
                        ON CONFLICT (id) DO NOTHING
                        """
                    )
                    cursor.execute("""
                        INSERT INTO release_versions (module_id, version, vulnerability_status)
                        VALUES
                          ('backend-api', 'v2.4.1', 'passed'), ('backend-api', 'v2.4.0', 'passed'), ('backend-api', 'v2.3.8', 'failed'),
                          ('web-client', 'v1.9.2', 'passed'), ('web-client', 'v1.9.1', 'passed')
                        ON CONFLICT (module_id, version) DO NOTHING
                    """)
                    cursor.execute("SELECT count(*) AS total FROM production_requests")
                    if int(cursor.fetchone()["total"]) == 0:
                        cursor.execute(
                            """
                            INSERT INTO production_requests (module_id, requested_by, version, status, comment)
                            VALUES
                              ('backend-api', 'TrungTT', 'v2.4.1', 'waiting_approval', NULL),
                              ('web-client', 'HaiNM', 'v1.9.2', 'approved', 'approved in previous release'),
                              ('alert-correlator', 'MinhNV', 'v0.8.4', 'blocked', 'blocked by vulnerability scan')
                            """
                        )
            return True
        except Exception:
            return False

    def load(self) -> dict[str, list[dict[str, Any]]] | None:
        try:
            with self._connect() as connection:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT id, unit, description, owner, status FROM systems ORDER BY created_at, id")
                    systems = list(cursor.fetchall())
                    cursor.execute("SELECT id, system_id, application_id, runtime, name, module_type, description FROM modules ORDER BY created_at, id")
                    modules = list(cursor.fetchall())
                    cursor.execute("SELECT module_id, version FROM release_versions ORDER BY created_at, version")
                    versions = list(cursor.fetchall())
                    cursor.execute("SELECT id, module_id, version, requested_by, status, deployment_id, comment FROM production_requests ORDER BY created_at, id")
                    requests = list(cursor.fetchall())
            return {"systems": systems, "modules": modules, "versions": versions, "requests": requests}
        except Exception:
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
                    INSERT INTO modules (id, system_id, application_id, runtime, name, module_type, description)
                    VALUES (%(id)s, %(system_id)s, %(application_id)s, %(runtime)s, %(name)s, %(module_type)s, %(description)s)
                    """,
                    record,
                )

    def update_request(self, request_id: str, status: str, comment: str | None) -> None:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE production_requests SET status = %s, comment = %s WHERE id = %s",
                    (status, comment, UUID(request_id)),
                )
