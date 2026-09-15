"""What one transaction can read and write.

Every method here answers from the store, inside the caller's transaction. There is no
process-local cache behind any of them, which is what makes "PostgreSQL is canonical"
a property of the code rather than a claim in a document.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from ..domain.models import (
    NotificationRecord,
    NotificationStatus,
    Application,
    ConfigRevisionStatus,
    DeliveryEvent,
    Deployment,
    ModuleConfigRevision,
    PipelineRun,
    PipelineStage,
    ScmIntegration,
    ScmProviderType,
    ScmWebhookDelivery,
    SecurityWaiver,
    ServerHealthRecord,
    ServerMaintenanceState,
    ServerTelemetry,
    StageDefinition,
)
from ..persistence import AuditRecord, IdempotencyRow, UnitOfWork
from .records import (
    BreakGlassRecord,
    CatalogServiceRecord,
    CatalogTemplateRecord,
    DeploymentLease,
    ModuleRow,
    PolicyDecisionRecord,
    PreviewEnvironmentRecord,
    RequestRow,
    ResourceQuotaRecord,
    ResourceRequestRecord,
    SecurityExceptionRecord,
    ServiceDependencyRecord,
    SystemRow,
    VersionRow,
)


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

    # --------------------------------------------------------------- deployment leases

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
        """Claim a target exclusively, or None when someone unexpired already holds it."""

    def active_deployment_lease(
        self, *, application_id: UUID, environment: str, target: str
    ) -> DeploymentLease | None: ...

    def deployment_lease(self, deployment_id: UUID) -> DeploymentLease | None: ...

    def heartbeat_deployment_lease(
        self, lease_id: UUID, *, ttl_seconds: int, now: datetime
    ) -> bool: ...

    def release_deployment_lease(self, lease_id: UUID, *, reason: str, now: datetime) -> bool: ...

    def expired_deployment_leases(
        self, now: datetime, limit: int = 100
    ) -> tuple[DeploymentLease, ...]: ...

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

    def insert_portal_version(self, row: VersionRow) -> None: ...

    def upsert_portal_version(self, row: VersionRow) -> None: ...

    def insert_version_ci_report(
        self, module_id: str, version: str, report: dict[str, Any], recorded_by: str = "netCI Pipeline"
    ) -> None: ...

    def latest_version_ci_report(self, module_id: str, version: str) -> dict[str, Any] | None: ...

    def insert_portal_request(self, row: RequestRow) -> None: ...

    def update_portal_request(
        self,
        request_id: str,
        *,
        status: str,
        comment: str | None,
        deployment_id: UUID | None = None,
        release_plan: dict[str, Any] | None = None,
    ) -> None: ...

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
    ) -> None: ...

    def update_deployment_traffic(
        self,
        deployment_id: UUID,
        *,
        strategy: str | None = None,
        traffic_weight: int,
        canary_step: int = 0,
        active_color: str | None = None,
    ) -> None: ...

    # ------------------------------------------------- SCM integrations & webhooks

    def scm_integration(self, integration_id: UUID) -> ScmIntegration | None: ...

    def scm_integration_for_application(
        self, application_id: UUID, provider: ScmProviderType | None = None
    ) -> ScmIntegration | None: ...

    def scm_integration_for_repository(
        self, provider: ScmProviderType, repository_identity: str
    ) -> ScmIntegration | None: ...

    def upsert_scm_integration(self, integration: ScmIntegration) -> None: ...

    def record_scm_webhook_delivery(self, delivery: ScmWebhookDelivery) -> bool:
        """Atomically record webhook delivery ID. Returns False if duplicate already exists."""
        ...

    def scm_webhook_delivery(self, delivery_id: str) -> ScmWebhookDelivery | None: ...

    # ------------------------------------------------------------- pipeline stages

    def record_pipeline_stage(self, stage: PipelineStage) -> PipelineStage: ...

    def pipeline_stages(self, pipeline_run_id: UUID) -> tuple[PipelineStage, ...]: ...

    # ---------------------------------------- versioned config & server health

    def config_revisions(self, module_id: str) -> tuple[ModuleConfigRevision, ...]: ...

    def config_revision(self, revision_id: UUID) -> ModuleConfigRevision | None: ...

    def config_revision_by_number(
        self, module_id: str, revision_number: int
    ) -> ModuleConfigRevision | None: ...

    def active_config_revision(self, module_id: str) -> ModuleConfigRevision | None: ...

    def record_config_revision(
        self, revision: ModuleConfigRevision
    ) -> ModuleConfigRevision: ...

    def update_config_revision_status(
        self,
        revision_id: UUID,
        status: ConfigRevisionStatus,
        approved_by: str | None = None,
        approved_at: datetime | None = None,
        rejection_reason: str | None = None,
    ) -> ModuleConfigRevision | None: ...

    def replace_portal_module_config(
        self,
        module_id: str,
        *,
        deployment_config: list[dict[str, Any]],
        pipeline_config: dict[str, Any],
    ) -> None:
        """Copy an activated revision into the module live configuration columns."""

    def set_module_active_revision(
        self, module_id: str, revision_id: UUID, expected_config_version: int
    ) -> bool:
        """Advance module's active revision using compare-and-set. Returns False if conflict."""
        ...

    def server_health(self, server_name: str) -> ServerHealthRecord | None: ...

    def list_server_health(self) -> tuple[ServerHealthRecord, ...]: ...

    def record_server_health(self, record: ServerHealthRecord) -> None: ...

    # ----------------------------------------------- notifications & outbox

    def record_notification(self, notification: NotificationRecord) -> NotificationRecord: ...

    def notification(self, notification_id: UUID) -> NotificationRecord | None: ...

    def pending_notifications(
        self, limit: int = 100, now: datetime | None = None
    ) -> tuple[NotificationRecord, ...]: ...

    def update_notification_status(
        self,
        notification_id: UUID,
        status: NotificationStatus,
        attempt: int,
        next_attempt_at: datetime,
        last_error: str | None = None,
        delivered_at: datetime | None = None,
    ) -> NotificationRecord | None: ...

    def notifications_paginated(
        self, status: NotificationStatus | None = None, limit: int = 50, cursor: str | None = None
    ) -> tuple[tuple[NotificationRecord, ...], str | None, bool]: ...

    # --------------------------------------------------- cursor pagination

    def pipeline_runs_paginated(
        self, application_id: UUID | None = None, limit: int = 50, cursor: str | None = None
    ) -> tuple[tuple[PipelineRun, ...], str | None, bool]: ...

    def deployments_paginated(
        self, application_id: UUID | None = None, limit: int = 50, cursor: str | None = None
    ) -> tuple[tuple[Deployment, ...], str | None, bool]: ...

    def audit_records_paginated(
        self, application_id: UUID | None = None, limit: int = 50, cursor: str | None = None
    ) -> tuple[tuple[AuditRecord, ...], str | None, bool]: ...

    # ----------------------------------------------------------- retention

    def purge_expired_callback_tokens(self, now: datetime) -> int: ...

    def purge_completed_notifications(self, cutoff: datetime) -> int: ...

    def purge_old_delivery_events(self, cutoff: datetime) -> int: ...

    # ----------------------------------------------------------- governance & policy

    def record_policy_decision(self, decision: PolicyDecisionRecord) -> None: ...

    def policy_decisions_paginated(
        self,
        scope: str | None = None,
        target_type: str | None = None,
        target_id: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> tuple[tuple[PolicyDecisionRecord, ...], str | None, bool]: ...

    def insert_security_exception(self, exception: SecurityExceptionRecord) -> None: ...

    def security_exceptions(
        self, active_only: bool = False, now: datetime | None = None
    ) -> tuple[SecurityExceptionRecord, ...]: ...

    def revoke_security_exception(
        self, exception_id: UUID, revoked_by: str, revoked_at: datetime
    ) -> bool: ...

    def insert_security_waiver(self, waiver: SecurityWaiver) -> None: ...

    def security_waivers(
        self, module_id: str | None = None, active_only: bool = True
    ) -> tuple[SecurityWaiver, ...]: ...

    def get_active_waiver(
        self, cve_id: str, module_id: str | None = None
    ) -> SecurityWaiver | None: ...

    def revoke_security_waiver(self, waiver_id: UUID) -> bool: ...

    def upsert_server_maintenance(self, state: ServerMaintenanceState) -> None: ...

    def get_server_maintenance(self, server_name: str) -> ServerMaintenanceState | None: ...

    def list_server_maintenance(self) -> tuple[ServerMaintenanceState, ...]: ...

    def upsert_server_telemetry(self, telemetry: ServerTelemetry) -> None: ...

    def stage_catalog(self) -> tuple[StageDefinition, ...]: ...

    def stage_definition(self, stage_id: str) -> StageDefinition | None: ...

    def upsert_stage_definition(self, stage: StageDefinition) -> None: ...

    def delete_stage_definition(self, stage_id: str) -> bool: ...

    def get_server_telemetry(self, server_name: str) -> ServerTelemetry | None: ...

    def insert_break_glass_request(self, record: BreakGlassRecord) -> None: ...

    def break_glass_request(self, request_id: UUID) -> BreakGlassRecord | None: ...

    def approve_break_glass_request(
        self, request_id: UUID, approved_by: str, approved_at: datetime, expires_at: datetime
    ) -> BreakGlassRecord | None: ...

    def active_break_glass(
        self, target_type: str, target_id: str, now: datetime
    ) -> BreakGlassRecord | None: ...

    def get_resource_quota(self, scope: str, scope_id: str) -> ResourceQuotaRecord | None: ...

    def set_resource_quota(self, record: ResourceQuotaRecord) -> None: ...

    # ----------------------------------------------- catalog & self-service

    def insert_catalog_service(self, service: CatalogServiceRecord) -> None: ...

    def update_catalog_service(self, service: CatalogServiceRecord) -> None: ...

    def catalog_service(self, service_id: str) -> CatalogServiceRecord | None: ...

    def list_catalog_services(
        self,
        owning_team: str | None = None,
        tier: str | None = None,
        lifecycle: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> tuple[tuple[CatalogServiceRecord, ...], str | None, bool]: ...

    def insert_service_dependency(self, dep: ServiceDependencyRecord) -> None: ...

    def delete_service_dependency(self, source_service_id: str, target_service_id: str) -> bool: ...

    def service_dependencies(self, service_id: str) -> tuple[ServiceDependencyRecord, ...]: ...

    def insert_catalog_template(self, template: CatalogTemplateRecord) -> None: ...

    def catalog_template(
        self, template_id: str, version: str | None = None
    ) -> CatalogTemplateRecord | None: ...

    def list_catalog_templates(
        self, category: str | None = None, include_deprecated: bool = False
    ) -> tuple[CatalogTemplateRecord, ...]: ...

    def insert_preview_environment(self, preview: PreviewEnvironmentRecord) -> None: ...

    def update_preview_environment_status(
        self, preview_id: str, status: str, destroyed_at: datetime | None = None
    ) -> PreviewEnvironmentRecord | None: ...

    def preview_environment(self, preview_id: str) -> PreviewEnvironmentRecord | None: ...

    def list_preview_environments(
        self, application_id: UUID | None = None, status: str | None = None
    ) -> tuple[PreviewEnvironmentRecord, ...]: ...

    def expired_preview_environments(self, now: datetime) -> tuple[PreviewEnvironmentRecord, ...]: ...

    def insert_resource_request(self, request: ResourceRequestRecord) -> None: ...

    def update_resource_request(
        self,
        request_id: UUID,
        *,
        status: str,
        status_reason: str | None = None,
        provider: str | None = None,
        outputs: dict[str, Any] | None = None,
        approved_by: str | None = None,
    ) -> ResourceRequestRecord | None: ...

    def resource_request(self, request_id: UUID) -> ResourceRequestRecord | None: ...

    def list_resource_requests(
        self,
        application_id: UUID | None = None,
        team_id: str | None = None,
        environment: str | None = None,
        status: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> tuple[tuple[ResourceRequestRecord, ...], str | None, bool]: ...


