"""What one transaction can read and write.

Every method here answers from the store, inside the caller's transaction. There is no
process-local cache behind any of them, which is what makes "PostgreSQL is canonical"
a property of the code rather than a claim in a document.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from ..domain.models import Application, DeliveryEvent, Deployment, PipelineRun
from ..persistence import AuditRecord, IdempotencyRow, UnitOfWork
from .records import ModuleRow, RequestRow, SystemRow, VersionRow


class PlatformSession(Protocol):
    # ------------------------------------------------------------- delivery reads

    def application(self, application_id: UUID) -> Application | None: ...

    def application_by_name(self, name: str) -> Application | None: ...

    def applications(self) -> tuple[Application, ...]: ...

    def pipeline_run(self, pipeline_run_id: UUID) -> PipelineRun | None: ...

    def pipeline_runs(self, application_id: UUID | None = None) -> tuple[PipelineRun, ...]: ...

    def deployment(self, deployment_id: UUID) -> Deployment | None: ...

    def deployments(
        self,
        application_id: UUID | None = None,
        pipeline_run_id: UUID | None = None,
    ) -> tuple[Deployment, ...]: ...

    def pipeline_logs(self, pipeline_run_id: UUID) -> tuple[str, ...]: ...

    def delivery_events(self, application_id: UUID | None = None) -> tuple[DeliveryEvent, ...]: ...

    def security_evidence(self, pipeline_run_id: UUID) -> dict[str, Any] | None: ...

    def audit_records(self, application_ids: set[UUID] | None = None) -> tuple[AuditRecord, ...]: ...

    def idempotency(self, scope: str, idempotency_key: str) -> IdempotencyRow | None: ...

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
        """Record a single-use callback token, returning False if it was already used."""

    def callback_token_used(self, jti: str) -> bool: ...

    # ------------------------------------------------------------ delivery writes

    def apply(self, unit: UnitOfWork) -> None:
        """Write one unit of work, enforcing the version each record was read at."""

    # --------------------------------------------------------------- portal reads

    def portal_system(self, system_id: str) -> SystemRow | None: ...

    def portal_systems(self) -> tuple[SystemRow, ...]: ...

    def portal_module(self, module_id: str) -> ModuleRow | None: ...

    def portal_modules(self, system_id: str | None = None) -> tuple[ModuleRow, ...]: ...

    def portal_module_for_application(self, application_id: UUID) -> ModuleRow | None: ...

    def portal_versions(self, module_id: str) -> tuple[VersionRow, ...]: ...

    def portal_version(self, module_id: str, version: str) -> VersionRow | None: ...

    def portal_requests(self) -> tuple[RequestRow, ...]: ...

    def portal_request(self, request_id: str) -> RequestRow | None: ...

    def portal_request_by_idempotency_key(self, idempotency_key: str) -> RequestRow | None: ...

    def portal_request_for_deployment(self, deployment_id: UUID) -> RequestRow | None: ...

    def portal_modules_referenced_by_requests(self) -> set[str]: ...

    # -------------------------------------------------------------- portal writes

    def insert_portal_system(self, row: SystemRow) -> None: ...

    def delete_portal_system(self, system_id: str) -> None: ...

    def insert_portal_module(self, row: ModuleRow) -> None: ...

    def update_portal_module(
        self, module_id: str, *, name: str, module_type: str, description: str
    ) -> None: ...

    def delete_portal_module(self, module_id: str) -> None: ...

    def upsert_portal_version(self, row: VersionRow) -> None: ...

    def insert_portal_request(self, row: RequestRow) -> None: ...

    def update_portal_request(
        self,
        request_id: str,
        *,
        status: str,
        comment: str | None,
        deployment_id: UUID | None = None,
    ) -> None: ...
