"""In-memory platform store for unit tests and explicit local mode.

This is a test adapter, not a fallback. `build_database` selects it only when
`DATABASE_URL` is absent *and* `NETCI_ENVIRONMENT=local`; any other runtime refuses to
start rather than accept approvals it will lose on the next restart.

It stages every write and discards the staged state if the transaction raises, so a test
that injects a fault between two writes observes the same all-or-nothing outcome a real
PostgreSQL transaction gives it. A single lock serializes transactions, which makes the
adapter honest about isolation instead of interleaving partial writes.
"""

from __future__ import annotations

import base64
import copy
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

from ..stage_catalog import BUILTIN_STAGES
from ..domain.models import (
    AgentCommand,
    AgentConnection,
    Application,
    ConfigRevisionStatus,
    DeliveryEvent,
    Deployment,
    DeploymentStatus,
    ModuleConfigRevision,
    NotificationRecord,
    NotificationStatus,
    PipelineRun,
    PipelineStatus,
    PipelineStage,
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
    RequestRow,
    ResourceQuotaRecord,
    ResourceRequestRecord,
    SecurityExceptionRecord,
    ServiceDependencyRecord,
    SystemRow,
    VersionRow,
)


@dataclass
class _State:
    applications: dict[UUID, Application] = field(default_factory=dict)
    runs: dict[UUID, PipelineRun] = field(default_factory=dict)
    deployments: dict[UUID, Deployment] = field(default_factory=dict)
    logs: dict[UUID, list[str]] = field(default_factory=dict)
    events: list[DeliveryEvent] = field(default_factory=list)
    audit: list[AuditRecord] = field(default_factory=list)
    evidence: dict[UUID, dict[str, Any]] = field(default_factory=dict)
    idempotency: dict[tuple[str, str], IdempotencyRow] = field(default_factory=dict)
    systems: dict[str, SystemRow] = field(default_factory=dict)
    modules: dict[str, ModuleRow] = field(default_factory=dict)
    versions: dict[str, list[VersionRow]] = field(default_factory=dict)
    requests: dict[str, RequestRow] = field(default_factory=dict)
    callback_tokens: dict[str, tuple[str, str, Any]] = field(default_factory=dict)
    leases: dict[UUID, DeploymentLease] = field(default_factory=dict)
    fencing_counters: dict[tuple[UUID, str, str], int] = field(default_factory=dict)
    log_sequences: dict[UUID, int] = field(default_factory=dict)
    ci_reports: dict[tuple[str, str], list[dict[str, Any]]] = field(default_factory=dict)
    scm_integrations: dict[UUID, ScmIntegration] = field(default_factory=dict)
    scm_deliveries: dict[str, ScmWebhookDelivery] = field(default_factory=dict)
    stages: dict[tuple[UUID, str, int], PipelineStage] = field(default_factory=dict)
    config_revisions: dict[UUID, ModuleConfigRevision] = field(default_factory=dict)
    server_health: dict[str, ServerHealthRecord] = field(default_factory=dict)
    notifications: dict[UUID, NotificationRecord] = field(default_factory=dict)
    policy_decisions: list[PolicyDecisionRecord] = field(default_factory=list)
    security_exceptions: dict[UUID, SecurityExceptionRecord] = field(default_factory=dict)
    break_glass_requests: dict[UUID, BreakGlassRecord] = field(default_factory=dict)
    resource_quotas: dict[tuple[str, str], ResourceQuotaRecord] = field(default_factory=dict)
    catalog_services: dict[str, CatalogServiceRecord] = field(default_factory=dict)
    service_dependencies: dict[UUID, ServiceDependencyRecord] = field(default_factory=dict)
    catalog_templates: dict[tuple[str, str], CatalogTemplateRecord] = field(default_factory=dict)
    preview_environments: dict[str, PreviewEnvironmentRecord] = field(default_factory=dict)
    resource_requests: dict[UUID, ResourceRequestRecord] = field(default_factory=dict)
    security_waivers: dict[UUID, SecurityWaiver] = field(default_factory=dict)
    server_maintenance: dict[str, ServerMaintenanceState] = field(default_factory=dict)
    server_telemetry: dict[str, ServerTelemetry] = field(default_factory=dict)
    agent_connections: dict[str, AgentConnection] = field(default_factory=dict)
    agent_commands: dict[UUID, AgentCommand] = field(default_factory=dict)
    stage_catalog: dict[str, StageDefinition] = field(default_factory=lambda: {s.id: s for s in BUILTIN_STAGES})

    def copy(self) -> "_State":
        return _State(
            applications=dict(self.applications),
            runs=dict(self.runs),
            deployments=dict(self.deployments),
            logs={key: list(value) for key, value in self.logs.items()},
            events=list(self.events),
            audit=list(self.audit),
            evidence={key: copy.deepcopy(value) for key, value in self.evidence.items()},
            idempotency=dict(self.idempotency),
            systems=dict(self.systems),
            modules=dict(self.modules),
            versions={key: list(value) for key, value in self.versions.items()},
            requests=dict(self.requests),
            callback_tokens=dict(self.callback_tokens),
            leases=dict(self.leases),
            fencing_counters=dict(self.fencing_counters),
            log_sequences=dict(self.log_sequences),
            ci_reports={key: list(value) for key, value in self.ci_reports.items()},
            scm_integrations=dict(self.scm_integrations),
            scm_deliveries=dict(self.scm_deliveries),
            stages=dict(self.stages),
            config_revisions=dict(self.config_revisions),
            server_health=dict(self.server_health),
            notifications=dict(self.notifications),
            policy_decisions=list(self.policy_decisions),
            security_exceptions=dict(self.security_exceptions),
            break_glass_requests=dict(self.break_glass_requests),
            resource_quotas=dict(self.resource_quotas),
            catalog_services=dict(self.catalog_services),
            service_dependencies=dict(self.service_dependencies),
            catalog_templates=dict(self.catalog_templates),
            preview_environments=dict(self.preview_environments),
            resource_requests=dict(self.resource_requests),
            security_waivers=dict(self.security_waivers),
            server_maintenance=dict(self.server_maintenance),
            server_telemetry=dict(self.server_telemetry),
            agent_connections=dict(self.agent_connections),
            agent_commands=dict(self.agent_commands),
            stage_catalog=dict(self.stage_catalog),
        )


class InMemorySession:
    def __init__(self, state: _State) -> None:
        self._state = state

    # ------------------------------------------------------------- delivery reads

    def application(self, application_id: UUID) -> Application | None:
        return self._state.applications.get(application_id)

    def application_by_name(self, name: str) -> Application | None:
        return next((item for item in self._state.applications.values() if item.name == name), None)

    def applications(self) -> tuple[Application, ...]:
        return tuple(self._state.applications.values())

    def pipeline_run(self, pipeline_run_id: UUID) -> PipelineRun | None:
        return self._state.runs.get(pipeline_run_id)

    def pipeline_runs(self, application_id: UUID | None = None) -> tuple[PipelineRun, ...]:
        runs = tuple(self._state.runs.values())
        if application_id is None:
            return runs
        return tuple(run for run in runs if run.application_id == application_id)

    def pipeline_run_by_artifact_digest(self, artifact_digest: str) -> PipelineRun | None:
        matches = [r for r in self._state.runs.values() if r.artifact_digest == artifact_digest]
        if not matches:
            return None
        # Same tie-break as PostgreSQL: the newest run carrying the digest.
        return max(matches, key=lambda r: (r.created_at, str(r.id)))

    def digest_in_service(self, application_id: UUID, environment: str) -> str | None:
        settled = [
            d for d in self._state.deployments.values()
            if d.application_id == application_id
            and d.environment.value == environment
            and d.status in {DeploymentStatus.HEALTHY, DeploymentStatus.ROLLED_BACK}
        ]
        if not settled:
            return None
        return max(settled, key=lambda d: d.updated_at).artifact_digest

    def runs_awaiting_ci_result(self, limit: int = 50) -> tuple[PipelineRun, ...]:
        ordered = sorted(self._state.runs.values(), key=lambda r: (r.created_at, str(r.id)))
        return tuple(
            run for run in ordered
            if run.status in {PipelineStatus.QUEUED, PipelineStatus.RUNNING}
            and not run.artifact_digest
        )[:limit]

    def deployments_with_status(
        self, status: DeploymentStatus, limit: int = 50
    ) -> tuple[Deployment, ...]:
        ordered = sorted(self._state.deployments.values(), key=lambda d: (d.created_at, str(d.id)))
        return tuple(d for d in ordered if d.status == status)[:limit]

    def deployment(self, deployment_id: UUID) -> Deployment | None:
        return self._state.deployments.get(deployment_id)

    def deployments(
        self,
        application_id: UUID | None = None,
        pipeline_run_id: UUID | None = None,
    ) -> tuple[Deployment, ...]:
        return tuple(
            item
            for item in self._state.deployments.values()
            if (application_id is None or item.application_id == application_id)
            and (pipeline_run_id is None or item.pipeline_run_id == pipeline_run_id)
        )

    def pipeline_logs(self, pipeline_run_id: UUID) -> tuple[str, ...]:
        return tuple(self._state.logs.get(pipeline_run_id, ()))

    def delivery_events(self, application_id: UUID | None = None) -> tuple[DeliveryEvent, ...]:
        if application_id is None:
            return tuple(self._state.events)
        return tuple(item for item in self._state.events if item.application_id == application_id)

    def security_evidence(self, pipeline_run_id: UUID) -> dict[str, Any] | None:
        found = self._state.evidence.get(pipeline_run_id)
        return dict(found) if found is not None else None

    def audit_records(self, application_ids: set[UUID] | None = None) -> tuple[AuditRecord, ...]:
        records = self._state.audit
        if application_ids is not None:
            records = [item for item in records if item.application_id in application_ids]
        return tuple(
            sorted(records, key=lambda item: (item.occurred_at, str(item.id)), reverse=True)
        )

    def idempotency(self, scope: str, idempotency_key: str) -> IdempotencyRow | None:
        return self._state.idempotency.get((scope, idempotency_key))

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
        if jti in self._state.callback_tokens:
            return False
        self._state.callback_tokens[jti] = (workload, operation, expires_at)
        return True

    def callback_token_used(self, jti: str) -> bool:
        return jti in self._state.callback_tokens

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
        key = (application_id, environment, target)
        for lease in list(self._state.leases.values()):
            if (
                lease.released_at is None
                and (lease.application_id, lease.environment, lease.target) == key
            ):
                if lease.expires_at > now:
                    return None
                self._state.leases[lease.id] = replace(
                    lease, released_at=now, release_reason="expired"
                )
        token = self._state.fencing_counters.get(key, 0) + 1
        self._state.fencing_counters[key] = token
        lease = DeploymentLease(
            id=uuid4(),
            application_id=application_id,
            environment=environment,
            target=target,
            deployment_id=deployment_id,
            owner=owner,
            fencing_token=token,
            acquired_at=now,
            heartbeat_at=now,
            expires_at=now + timedelta(seconds=max(1, ttl_seconds)),
        )
        self._state.leases[lease.id] = lease
        return lease

    def active_deployment_lease(
        self, *, application_id: UUID, environment: str, target: str
    ) -> DeploymentLease | None:
        return next(
            (
                lease
                for lease in self._state.leases.values()
                if lease.released_at is None
                and (lease.application_id, lease.environment, lease.target)
                == (application_id, environment, target)
            ),
            None,
        )

    def deployment_lease(self, deployment_id: UUID) -> DeploymentLease | None:
        found = [
            lease for lease in self._state.leases.values() if lease.deployment_id == deployment_id
        ]
        return max(found, key=lambda item: item.acquired_at) if found else None

    def heartbeat_deployment_lease(
        self, lease_id: UUID, *, ttl_seconds: int, now: datetime
    ) -> bool:
        lease = self._state.leases.get(lease_id)
        if lease is None or lease.released_at is not None:
            return False
        self._state.leases[lease_id] = replace(
            lease, heartbeat_at=now, expires_at=now + timedelta(seconds=max(1, ttl_seconds))
        )
        return True

    def release_deployment_lease(self, lease_id: UUID, *, reason: str, now: datetime) -> bool:
        lease = self._state.leases.get(lease_id)
        if lease is None or lease.released_at is not None:
            return False
        self._state.leases[lease_id] = replace(
            lease, released_at=now, release_reason=reason[:64]
        )
        return True

    def expired_deployment_leases(
        self, now: datetime, limit: int = 100
    ) -> tuple[DeploymentLease, ...]:
        found = sorted(
            (lease for lease in self._state.leases.values() if lease.is_expired(now)),
            key=lambda item: item.expires_at,
        )
        return tuple(found[:limit])

    # ------------------------------------------------------------ delivery writes

    def apply(self, unit: UnitOfWork) -> None:
        if unit.is_empty():
            return
        state = self._state
        for application in unit.applications:
            state.applications[application.id] = application
        for run, expected_version in unit.runs:
            current = state.runs.get(run.id)
            if expected_version is None:
                if current is not None:
                    raise ConcurrentModification(f"pipeline run {run.id} already exists")
            elif current is None or current.version != expected_version:
                raise ConcurrentModification(f"pipeline run {run.id} changed since it was read")
            state.runs[run.id] = run
            state.logs.setdefault(run.id, [])
        for deployment, expected_version in unit.deployments:
            current_deployment = state.deployments.get(deployment.id)
            if expected_version is None:
                if current_deployment is not None:
                    raise ConcurrentModification(f"deployment {deployment.id} already exists")
            elif current_deployment is None or current_deployment.version != expected_version:
                raise ConcurrentModification(f"deployment {deployment.id} changed since it was read")
            state.deployments[deployment.id] = deployment
        for run_id, lines in unit.logs:
            if not lines:
                continue
            start = state.log_sequences.get(run_id, 1)
            state.log_sequences[run_id] = start + len(lines)
            state.logs.setdefault(run_id, []).extend(line[:8000] for line in lines)
        state.events.extend(unit.events)
        state.audit.extend(unit.audit)
        for pipeline_run_id, _, _, evidence in unit.security_evidence:
            state.evidence[pipeline_run_id] = dict(evidence)
        for notification in unit.notifications:
            self.record_notification(notification)
        for row in unit.idempotency:
            key = (row.scope, row.idempotency_key)
            if key in state.idempotency:
                raise ConcurrentModification(
                    f"idempotency key {row.scope}/{row.idempotency_key} was committed concurrently"
                )
            state.idempotency[key] = row

    # --------------------------------------------------------------- portal reads

    def portal_system(self, system_id: str) -> SystemRow | None:
        return self._state.systems.get(system_id)

    def portal_systems(self) -> tuple[SystemRow, ...]:
        return tuple(self._state.systems.values())

    def portal_module(self, module_id: str) -> ModuleRow | None:
        return self._state.modules.get(module_id)

    def portal_modules(self, system_id: str | None = None) -> tuple[ModuleRow, ...]:
        return tuple(
            item
            for item in self._state.modules.values()
            if system_id is None or item.system_id == system_id
        )

    def portal_module_for_application(self, application_id: UUID) -> ModuleRow | None:
        return next(
            (item for item in self._state.modules.values() if item.application_id == application_id),
            None,
        )

    def portal_versions(self, module_id: str) -> tuple[VersionRow, ...]:
        rows = self._state.versions.get(module_id, ())
        result: list[VersionRow] = []
        for item in rows:
            meta = dict(item.metadata)
            reports = self._state.ci_reports.get((module_id, item.version))
            if reports:
                meta["ciReport"] = reports[-1]
            result.append(VersionRow(item.module_id, item.version, meta))
        return tuple(result)

    def portal_version(self, module_id: str, version: str) -> VersionRow | None:
        item = next(
            (r for r in self._state.versions.get(module_id, ()) if r.version == version),
            None,
        )
        if item is None:
            return None
        meta = dict(item.metadata)
        reports = self._state.ci_reports.get((module_id, version))
        if reports:
            meta["ciReport"] = reports[-1]
        return VersionRow(item.module_id, item.version, meta)

    def portal_requests(self) -> tuple[RequestRow, ...]:
        return tuple(self._state.requests.values())

    def portal_request(self, request_id: str) -> RequestRow | None:
        return self._state.requests.get(str(request_id))

    def portal_request_by_idempotency_key(self, idempotency_key: str) -> RequestRow | None:
        return next(
            (
                item
                for item in self._state.requests.values()
                if item.idempotency_key == idempotency_key
            ),
            None,
        )

    def portal_request_for_deployment(self, deployment_id: UUID) -> RequestRow | None:
        return next(
            (item for item in self._state.requests.values() if item.deployment_id == deployment_id),
            None,
        )

    def portal_modules_referenced_by_requests(self) -> set[str]:
        return {
            member.module_id
            for request in self._state.requests.values()
            for member in request.modules
        }

    # -------------------------------------------------------------- portal writes

    def insert_portal_system(self, row: SystemRow) -> None:
        if row.id in self._state.systems:
            raise ConcurrentModification(f"system {row.id} already exists")
        self._state.systems[row.id] = row

    def delete_portal_system(self, system_id: str) -> None:
        referenced = self.portal_modules_referenced_by_requests()
        for module in self.portal_modules(system_id):
            if module.id in referenced:
                raise StillReferenced(
                    f"system {system_id} has a module that a production request still references"
                )
            self._state.modules.pop(module.id, None)
        self._state.systems.pop(system_id, None)

    def insert_portal_module(self, row: ModuleRow) -> None:
        if row.id in self._state.modules:
            raise ConcurrentModification(f"module {row.id} already exists")
        self._state.modules[row.id] = row

    def update_portal_module(
        self, module_id: str, *, name: str, module_type: str, description: str
    ) -> None:
        current = self._state.modules.get(module_id)
        if current is None:
            raise KeyError("module not found")
        self._state.modules[module_id] = replace(
            current, name=name, module_type=module_type, description=description
        )

    def delete_portal_module(self, module_id: str) -> None:
        if module_id in self.portal_modules_referenced_by_requests():
            raise StillReferenced(
                f"module {module_id} is still referenced by a production request"
            )
        self._state.modules.pop(module_id, None)

    def insert_portal_version(self, row: VersionRow) -> None:
        existing = self._state.versions.setdefault(row.module_id, [])
        for item in existing:
            if item.version == row.version:
                if item.metadata == row.metadata:
                    return
                raise VersionConflict(
                    f"release version '{row.version}' already exists for module '{row.module_id}'"
                )
        existing.insert(0, row)

    def upsert_portal_version(self, row: VersionRow) -> None:
        self.insert_portal_version(row)

    def insert_version_ci_report(
        self, module_id: str, version: str, report: dict[str, Any], recorded_by: str = "netCI Pipeline"
    ) -> None:
        self._state.ci_reports.setdefault((module_id, version), []).append(dict(report))

    def latest_version_ci_report(self, module_id: str, version: str) -> dict[str, Any] | None:
        reports = self._state.ci_reports.get((module_id, version))
        return reports[-1] if reports else None

    def insert_portal_request(self, row: RequestRow) -> None:
        if row.idempotency_key and any(
            item.idempotency_key == row.idempotency_key for item in self._state.requests.values()
        ):
            raise ConcurrentModification(
                f"production request idempotency key {row.idempotency_key} was committed concurrently"
            )
        self._state.requests[row.id] = row

    def update_portal_request(
        self,
        request_id: str,
        *,
        status: str,
        comment: str | None,
        deployment_id: UUID | None = None,
        release_plan: dict[str, Any] | None = None,
    ) -> None:
        current = self._state.requests.get(str(request_id))
        if current is None:
            return
        self._state.requests[str(request_id)] = replace(
            current,
            status=status,
            comment=comment,
            deployment_id=deployment_id if deployment_id is not None else current.deployment_id,
            release_plan=release_plan if release_plan is not None else current.release_plan,
        )

    def claim_portal_request(
        self,
        request_id: str,
        *,
        from_status: str,
        to_status: str,
        comment: str | None = None,
    ) -> bool:
        current = self._state.requests.get(str(request_id))
        if current is None or current.status != from_status:
            return False
        self._state.requests[str(request_id)] = replace(
            current,
            status=to_status,
            comment=comment if comment is not None else current.comment,
        )
        return True

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
        current = self._state.requests.get(str(request_id))
        if current is None:
            return
        new_modules = []
        for m in current.modules:
            if m.module_id == module_id:
                new_modules.append(
                    replace(
                        m,
                        status=status,
                        deployment_id=deployment_id if deployment_id is not None else m.deployment_id,
                        error_message=error_message if error_message is not None else m.error_message,
                        started_at=started_at if started_at is not None else m.started_at,
                        completed_at=completed_at if completed_at is not None else m.completed_at,
                    )
                )
            else:
                new_modules.append(m)
        self._state.requests[str(request_id)] = replace(current, modules=tuple(new_modules))

    def update_deployment_traffic(
        self,
        deployment_id: UUID,
        *,
        strategy: str | None = None,
        traffic_weight: int,
        canary_step: int = 0,
        active_color: str | None = None,
    ) -> None:
        current = self._state.deployments.get(deployment_id)
        if current is None:
            return
        self._state.deployments[deployment_id] = replace(
            current,
            strategy=strategy if strategy is not None else current.strategy,
            traffic_weight=traffic_weight,
            canary_step=canary_step,
            active_color=active_color if active_color is not None else current.active_color,
        )

    # ------------------------------------------------- SCM integrations & webhooks

    def scm_integration(self, integration_id: UUID) -> ScmIntegration | None:
        return self._state.scm_integrations.get(integration_id)

    def scm_integration_for_application(
        self, application_id: UUID, provider: ScmProviderType | None = None
    ) -> ScmIntegration | None:
        for integration in self._state.scm_integrations.values():
            if integration.application_id == application_id:
                if provider is None or integration.provider == provider:
                    return integration
        return None

    def scm_integration_for_repository(
        self, provider: ScmProviderType, repository_identity: str
    ) -> ScmIntegration | None:
        for integration in self._state.scm_integrations.values():
            if integration.provider == provider and integration.repository_identity == repository_identity:
                return integration
        return None

    def upsert_scm_integration(self, integration: ScmIntegration) -> None:
        for existing_id, existing in list(self._state.scm_integrations.items()):
            if (
                existing.application_id == integration.application_id
                and existing.provider == integration.provider
            ) or (
                existing.provider == integration.provider
                and existing.repository_identity == integration.repository_identity
            ):
                updated = replace(
                    integration,
                    id=existing.id,
                    secret_token=integration.secret_token or existing.secret_token,
                    secret_token_hash=integration.secret_token_hash or existing.secret_token_hash,
                )
                self._state.scm_integrations[existing.id] = updated
                return
        self._state.scm_integrations[integration.id] = integration

    def record_scm_webhook_delivery(self, delivery: ScmWebhookDelivery) -> bool:
        if delivery.delivery_id in self._state.scm_deliveries:
            return False
        self._state.scm_deliveries[delivery.delivery_id] = delivery
        return True

    def scm_webhook_delivery(self, delivery_id: str) -> ScmWebhookDelivery | None:
        return self._state.scm_deliveries.get(delivery_id)

    # ------------------------------------------------------------- pipeline stages

    def record_pipeline_stage(self, stage: PipelineStage) -> PipelineStage:
        key = (stage.pipeline_run_id, stage.stage_id, stage.attempt)
        existing = self._state.stages.get(key)
        if existing:
            saved = replace(
                stage,
                id=existing.id,
                queued_at=stage.queued_at or existing.queued_at,
                started_at=stage.started_at or existing.started_at,
                completed_at=stage.completed_at or existing.completed_at,
                duration_ms=stage.duration_ms if stage.duration_ms is not None else existing.duration_ms,
                error_message=stage.error_message or existing.error_message,
                log_snippet=stage.log_snippet or existing.log_snippet,
                created_at=existing.created_at,
            )
        else:
            saved = stage
        self._state.stages[key] = saved
        return saved

    def pipeline_stages(self, pipeline_run_id: UUID) -> tuple[PipelineStage, ...]:
        matching = [
            stage
            for (run_id, _, _), stage in self._state.stages.items()
            if run_id == pipeline_run_id
        ]
        matching.sort(key=lambda s: (s.created_at, s.attempt, s.stage_id))
        return tuple(matching)

    # ---------------------------------------- versioned config & server health

    def config_revisions(self, module_id: str) -> tuple[ModuleConfigRevision, ...]:
        revs = [r for r in self._state.config_revisions.values() if r.module_id == module_id]
        revs.sort(key=lambda r: r.revision_number, reverse=True)
        return tuple(revs)

    def config_revision(self, revision_id: UUID) -> ModuleConfigRevision | None:
        return self._state.config_revisions.get(revision_id)

    def config_revision_by_number(
        self, module_id: str, revision_number: int
    ) -> ModuleConfigRevision | None:
        for r in self._state.config_revisions.values():
            if r.module_id == module_id and r.revision_number == revision_number:
                return r
        return None

    def active_config_revision(self, module_id: str) -> ModuleConfigRevision | None:
        mod = self._state.modules.get(module_id)
        if not mod or not mod.active_config_revision_id:
            return None
        return self._state.config_revisions.get(mod.active_config_revision_id)

    def record_config_revision(
        self, revision: ModuleConfigRevision
    ) -> ModuleConfigRevision:
        self._state.config_revisions[revision.id] = revision
        return revision

    def update_config_revision_status(
        self,
        revision_id: UUID,
        status: ConfigRevisionStatus,
        approved_by: str | None = None,
        approved_at: datetime | None = None,
        rejection_reason: str | None = None,
    ) -> ModuleConfigRevision | None:
        rev = self._state.config_revisions.get(revision_id)
        if not rev:
            return None
        updated = replace(
            rev,
            status=status,
            approved_by=approved_by if approved_by is not None else rev.approved_by,
            approved_at=approved_at if approved_at is not None else rev.approved_at,
            rejection_reason=rejection_reason if rejection_reason is not None else rev.rejection_reason,
        )
        self._state.config_revisions[revision_id] = updated
        return updated

    def replace_portal_module_config(
        self,
        module_id: str,
        *,
        deployment_config: list[dict[str, Any]],
        pipeline_config: dict[str, Any],
    ) -> None:
        current = self._state.modules.get(module_id)
        if current is None:
            raise KeyError("module not found")
        self._state.modules[module_id] = replace(
            current,
            deployment_config=list(deployment_config),
            pipeline_config=dict(pipeline_config),
        )

    def set_module_active_revision(
        self, module_id: str, revision_id: UUID, expected_config_version: int
    ) -> bool:
        mod = self._state.modules.get(module_id)
        if not mod:
            return False
        if mod.config_version != expected_config_version:
            return False
        rev = self._state.config_revisions.get(revision_id)
        if not rev:
            return False
        self._state.modules[module_id] = replace(
            mod,
            active_config_revision_id=revision_id,
            config_version=mod.config_version + 1,
            pipeline_config=dict(rev.pipeline_config),
            deployment_config=list(rev.deployment_config),
        )
        return True

    def server_health(self, server_name: str) -> ServerHealthRecord | None:
        return self._state.server_health.get(server_name)

    def list_server_health(self) -> tuple[ServerHealthRecord, ...]:
        records = list(self._state.server_health.values())
        records.sort(key=lambda r: r.server_name)
        return tuple(records)

    def record_server_health(self, record: ServerHealthRecord) -> None:
        self._state.server_health[record.server_name] = record

    # ----------------------------------------------- notifications & outbox

    def record_notification(self, notification: NotificationRecord) -> NotificationRecord:
        self._state.notifications[notification.id] = notification
        return notification

    def notification(self, notification_id: UUID) -> NotificationRecord | None:
        return self._state.notifications.get(notification_id)

    def pending_notifications(
        self, limit: int = 100, now: datetime | None = None
    ) -> tuple[NotificationRecord, ...]:
        ts = now or datetime.now(timezone.utc)
        records = [
            n for n in self._state.notifications.values()
            if n.status in (NotificationStatus.PENDING, NotificationStatus.FAILED)
            and n.next_attempt_at <= ts
        ]
        records.sort(key=lambda n: (n.next_attempt_at, n.id))
        return tuple(records[:limit])

    def update_notification_status(
        self,
        notification_id: UUID,
        status: NotificationStatus,
        attempt: int,
        next_attempt_at: datetime,
        last_error: str | None = None,
        delivered_at: datetime | None = None,
    ) -> NotificationRecord | None:
        rec = self._state.notifications.get(notification_id)
        if not rec:
            return None
        updated = replace(
            rec,
            status=status,
            attempt=attempt,
            next_attempt_at=next_attempt_at,
            last_attempt_at=datetime.now(timezone.utc),
            last_error=last_error,
            delivered_at=delivered_at or rec.delivered_at,
        )
        self._state.notifications[notification_id] = updated
        return updated

    def notifications_paginated(
        self, status: NotificationStatus | None = None, limit: int = 50, cursor: str | None = None
    ) -> tuple[tuple[NotificationRecord, ...], str | None, bool]:
        records = list(self._state.notifications.values())
        if status is not None:
            records = [r for r in records if r.status == status]
        records.sort(key=lambda r: (r.created_at, r.id), reverse=True)
        return self._paginate(records, limit, cursor, lambda r: (r.created_at, r.id))

    # --------------------------------------------------- cursor pagination

    def _paginate(self, items: list[Any], limit: int, cursor: str | None, key_fn: Any) -> tuple[tuple[Any, ...], str | None, bool]:
        if cursor:
            try:
                raw = base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8")
                ts_str, id_str = raw.split("|", 1)
                cursor_ts = datetime.fromisoformat(ts_str)
                if cursor_ts.tzinfo is None:  # see decode_cursor in store/postgres.py
                    cursor_ts = cursor_ts.replace(tzinfo=timezone.utc)
                filtered: list[Any] = []
                for it in items:
                    it_ts, it_id = key_fn(it)
                    if (it_ts, str(it_id)) < (cursor_ts, id_str):
                        filtered.append(it)
                items = filtered
            except Exception:
                pass
        has_more = len(items) > limit
        result = items[:limit]
        next_cursor = None
        if has_more and result:
            last_ts, last_id = key_fn(result[-1])
            payload = f"{last_ts.isoformat()}|{last_id}"
            next_cursor = base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii")
        return tuple(result), next_cursor, has_more

    def pipeline_runs_paginated(
        self, application_id: UUID | None = None, limit: int = 50, cursor: str | None = None
    ) -> tuple[tuple[PipelineRun, ...], str | None, bool]:
        runs = list(self._state.runs.values())
        if application_id is not None:
            runs = [r for r in runs if r.application_id == application_id]
        runs.sort(key=lambda r: (r.created_at, r.id), reverse=True)
        return self._paginate(runs, limit, cursor, lambda r: (r.created_at, r.id))

    def deployments_paginated(
        self, application_id: UUID | None = None, limit: int = 50, cursor: str | None = None
    ) -> tuple[tuple[Deployment, ...], str | None, bool]:
        deps = list(self._state.deployments.values())
        if application_id is not None:
            deps = [d for d in deps if d.application_id == application_id]
        deps.sort(key=lambda d: (d.created_at, d.id), reverse=True)
        return self._paginate(deps, limit, cursor, lambda d: (d.created_at, d.id))

    def audit_records_paginated(
        self, application_id: UUID | None = None, limit: int = 50, cursor: str | None = None
    ) -> tuple[tuple[AuditRecord, ...], str | None, bool]:
        records = list(self._state.audit)
        if application_id is not None:
            records = [a for a in records if a.application_id == application_id]
        records.sort(key=lambda a: (a.occurred_at, a.id), reverse=True)
        return self._paginate(records, limit, cursor, lambda a: (a.occurred_at, a.id))

    # ----------------------------------------------------------- retention

    def purge_expired_callback_tokens(self, now: datetime) -> int:
        to_del = [k for k, v in self._state.callback_tokens.items() if hasattr(v[2], "timestamp") and v[2] < now]
        for k in to_del:
            del self._state.callback_tokens[k]
        return len(to_del)

    def purge_completed_notifications(self, cutoff: datetime) -> int:
        to_del = [
            k for k, v in self._state.notifications.items()
            if v.status == NotificationStatus.DELIVERED and v.delivered_at and v.delivered_at < cutoff
        ]
        for k in to_del:
            del self._state.notifications[k]
        return len(to_del)

    def purge_old_pipeline_logs(self, cutoff: datetime) -> int:
        terminal = {PipelineStatus.SUCCEEDED, PipelineStatus.FAILED, PipelineStatus.CANCELLED, PipelineStatus.ROLLED_BACK}
        purged = 0
        for run_id, run in self._state.runs.items():
            if run.status in terminal and run.updated_at < cutoff and self._state.logs.get(run_id):
                purged += len(self._state.logs[run_id])
                self._state.logs[run_id] = []
        return purged

    def purge_old_delivery_events(self, cutoff: datetime) -> int:
        orig_len = len(self._state.events)
        self._state.events = [e for e in self._state.events if e.occurred_at >= cutoff]
        return orig_len - len(self._state.events)

    # ----------------------------------------------------------- governance & policy

    def record_policy_decision(self, decision: PolicyDecisionRecord) -> None:
        self._state.policy_decisions.append(decision)

    def policy_decisions_paginated(
        self,
        scope: str | None = None,
        target_type: str | None = None,
        target_id: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> tuple[tuple[PolicyDecisionRecord, ...], str | None, bool]:
        records = list(self._state.policy_decisions)
        if scope is not None:
            records = [r for r in records if r.scope == scope]
        if target_type is not None:
            records = [r for r in records if r.target_type == target_type]
        if target_id is not None:
            records = [r for r in records if r.target_id == target_id]
        records.sort(key=lambda r: (r.evaluated_at, r.id), reverse=True)
        return self._paginate(records, limit, cursor, lambda r: (r.evaluated_at, r.id))

    def insert_security_exception(self, exception: SecurityExceptionRecord) -> None:
        self._state.security_exceptions[exception.id] = exception

    def security_exceptions(
        self, active_only: bool = False, now: datetime | None = None
    ) -> tuple[SecurityExceptionRecord, ...]:
        records = list(self._state.security_exceptions.values())
        if active_only:
            ts = now or datetime.now(timezone.utc)
            records = [r for r in records if r.is_active(ts)]
        records.sort(key=lambda r: r.created_at, reverse=True)
        return tuple(records)

    def revoke_security_exception(
        self, exception_id: UUID, revoked_by: str, revoked_at: datetime
    ) -> bool:
        rec = self._state.security_exceptions.get(exception_id)
        if rec and rec.status == "active":
            self._state.security_exceptions[exception_id] = replace(
                rec, status="revoked", revoked_by=revoked_by, revoked_at=revoked_at
            )
            return True
        return False

    def insert_security_waiver(self, waiver: SecurityWaiver) -> None:
        self._state.security_waivers[waiver.id] = waiver

    def security_waivers(
        self, module_id: str | None = None, active_only: bool = True
    ) -> tuple[SecurityWaiver, ...]:
        records = list(self._state.security_waivers.values())
        if module_id is not None:
            records = [r for r in records if r.module_id is None or r.module_id == module_id]
        if active_only:
            records = [r for r in records if r.is_valid]
        records.sort(key=lambda r: r.created_at, reverse=True)
        return tuple(records)

    def get_active_waiver(
        self, cve_id: str, module_id: str | None = None
    ) -> SecurityWaiver | None:
        for w in self._state.security_waivers.values():
            if w.cve_id == cve_id and w.is_valid:
                if module_id is None or w.module_id is None or w.module_id == module_id:
                    return w
        return None

    def revoke_security_waiver(self, waiver_id: UUID) -> bool:
        w = self._state.security_waivers.get(waiver_id)
        if w and w.status == WaiverStatus.ACTIVE:
            self._state.security_waivers[waiver_id] = replace(w, status=WaiverStatus.REVOKED)
            return True
        return False

    def upsert_server_maintenance(self, state: ServerMaintenanceState) -> None:
        self._state.server_maintenance[state.server_name] = state

    def get_server_maintenance(self, server_name: str) -> ServerMaintenanceState | None:
        return self._state.server_maintenance.get(server_name)

    def list_server_maintenance(self) -> tuple[ServerMaintenanceState, ...]:
        return tuple(self._state.server_maintenance.values())

    def upsert_server_telemetry(self, telemetry: ServerTelemetry) -> None:
        self._state.server_telemetry[telemetry.server_name] = telemetry

    def get_server_telemetry(self, server_name: str) -> ServerTelemetry | None:
        return self._state.server_telemetry.get(server_name)

    # ------------------------------------------------------------ agent fleet

    def upsert_agent_connection(self, connection: AgentConnection) -> None:
        self._state.agent_connections[connection.hostname] = connection

    def touch_agent_connection(self, hostname: str, replica_id: str, seen_at: datetime) -> bool:
        current = self._state.agent_connections.get(hostname)
        if current is None or current.replica_id != replica_id:
            return False
        self._state.agent_connections[hostname] = replace(current, last_seen_at=seen_at)
        return True

    def delete_agent_connection(self, hostname: str, replica_id: str) -> bool:
        current = self._state.agent_connections.get(hostname)
        if current is None or current.replica_id != replica_id:
            return False
        del self._state.agent_connections[hostname]
        return True

    def list_agent_connections(self) -> tuple[AgentConnection, ...]:
        return tuple(sorted(self._state.agent_connections.values(), key=lambda c: c.hostname))

    def insert_agent_command(self, command: AgentCommand) -> None:
        self._state.agent_commands[command.id] = command

    def claim_agent_commands(self, replica_id: str, hostnames: list[str], now: datetime) -> tuple[AgentCommand, ...]:
        claimed = []
        for command in sorted(self._state.agent_commands.values(), key=lambda c: c.created_at):
            if command.status == "pending" and command.hostname in hostnames and command.expires_at > now:
                updated = replace(command, status="sent", claimed_by=replica_id, claimed_at=now)
                self._state.agent_commands[command.id] = updated
                claimed.append(updated)
        return tuple(claimed)

    def complete_agent_command(self, command_id: UUID, status: str, result: dict[str, Any], completed_at: datetime) -> bool:
        current = self._state.agent_commands.get(command_id)
        if current is None or current.status not in ("pending", "sent"):
            return False
        self._state.agent_commands[command_id] = replace(current, status=status, result=result, completed_at=completed_at)
        return True

    def agent_command(self, command_id: UUID) -> AgentCommand | None:
        return self._state.agent_commands.get(command_id)

    def expire_agent_commands(self, now: datetime) -> int:
        count = 0
        for command in list(self._state.agent_commands.values()):
            if command.status in ("pending", "sent") and command.expires_at <= now:
                self._state.agent_commands[command.id] = replace(command, status="expired", completed_at=now)
                count += 1
        return count

    def try_advisory_lock(self, key: int) -> bool:
        # One process, one loop: there is nobody to lose the race against.
        return True

    def stage_catalog(self) -> tuple[StageDefinition, ...]:
        return tuple(sorted(self._state.stage_catalog.values(), key=lambda s: (s.position, s.id)))

    def stage_definition(self, stage_id: str) -> StageDefinition | None:
        return self._state.stage_catalog.get(stage_id)

    def upsert_stage_definition(self, stage: StageDefinition) -> None:
        self._state.stage_catalog[stage.id] = stage

    def delete_stage_definition(self, stage_id: str) -> bool:
        return self._state.stage_catalog.pop(stage_id, None) is not None

    def insert_break_glass_request(self, record: BreakGlassRecord) -> None:
        self._state.break_glass_requests[record.id] = record

    def break_glass_request(self, request_id: UUID) -> BreakGlassRecord | None:
        return self._state.break_glass_requests.get(request_id)

    def approve_break_glass_request(
        self, request_id: UUID, approved_by: str, approved_at: datetime, expires_at: datetime
    ) -> BreakGlassRecord | None:
        rec = self._state.break_glass_requests.get(request_id)
        if rec and rec.status == "pending":
            updated = replace(
                rec,
                status="active",
                approved_by=approved_by,
                approved_at=approved_at,
                expires_at=expires_at,
            )
            self._state.break_glass_requests[request_id] = updated
            return updated
        return None

    def active_break_glass(
        self, target_type: str, target_id: str, now: datetime
    ) -> BreakGlassRecord | None:
        active = [
            r
            for r in self._state.break_glass_requests.values()
            if r.target_type == target_type
            and r.target_id == target_id
            and r.status == "active"
            and r.expires_at is not None
            and r.expires_at > now
        ]
        if not active:
            return None
        active.sort(key=lambda r: r.expires_at or now, reverse=True)
        return active[0]

    def get_resource_quota(self, scope: str, scope_id: str) -> ResourceQuotaRecord | None:
        return self._state.resource_quotas.get((scope, scope_id))

    def set_resource_quota(self, record: ResourceQuotaRecord) -> None:
        self._state.resource_quotas[(record.scope, record.scope_id)] = record

    # ----------------------------------------------- catalog & self-service

    def insert_catalog_service(self, service: CatalogServiceRecord) -> None:
        self._state.catalog_services[service.id] = service

    def update_catalog_service(self, service: CatalogServiceRecord) -> None:
        self._state.catalog_services[service.id] = service

    def catalog_service(self, service_id: str) -> CatalogServiceRecord | None:
        return self._state.catalog_services.get(service_id)

    def list_catalog_services(
        self,
        owning_team: str | None = None,
        tier: str | None = None,
        lifecycle: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> tuple[tuple[CatalogServiceRecord, ...], str | None, bool]:
        records = list(self._state.catalog_services.values())
        if owning_team:
            records = [r for r in records if r.owning_team == owning_team]
        if tier:
            records = [r for r in records if r.tier == tier]
        if lifecycle:
            records = [r for r in records if r.lifecycle == lifecycle]
        records.sort(key=lambda r: (r.created_at, r.id), reverse=True)
        return self._paginate(records, limit, cursor, lambda r: (r.created_at, r.id))

    def insert_service_dependency(self, dep: ServiceDependencyRecord) -> None:
        for existing_id, existing in list(self._state.service_dependencies.items()):
            if (
                existing.source_service_id == dep.source_service_id
                and existing.target_service_id == dep.target_service_id
            ):
                del self._state.service_dependencies[existing_id]
        self._state.service_dependencies[dep.id] = dep

    def delete_service_dependency(self, source_service_id: str, target_service_id: str) -> bool:
        found = False
        for existing_id, existing in list(self._state.service_dependencies.items()):
            if (
                existing.source_service_id == source_service_id
                and existing.target_service_id == target_service_id
            ):
                del self._state.service_dependencies[existing_id]
                found = True
        return found

    def service_dependencies(self, service_id: str) -> tuple[ServiceDependencyRecord, ...]:
        matched = [
            d
            for d in self._state.service_dependencies.values()
            if d.source_service_id == service_id or d.target_service_id == service_id
        ]
        matched.sort(key=lambda d: d.created_at)
        return tuple(matched)

    def insert_catalog_template(self, template: CatalogTemplateRecord) -> None:
        self._state.catalog_templates[(template.id, template.version)] = template

    def catalog_template(
        self, template_id: str, version: str | None = None
    ) -> CatalogTemplateRecord | None:
        if version:
            return self._state.catalog_templates.get((template_id, version))
        matching = [
            t for (tid, _), t in self._state.catalog_templates.items() if tid == template_id
        ]
        if not matching:
            return None
        matching.sort(key=lambda t: t.created_at, reverse=True)
        return matching[0]

    def list_catalog_templates(
        self, category: str | None = None, include_deprecated: bool = False
    ) -> tuple[CatalogTemplateRecord, ...]:
        templates = list(self._state.catalog_templates.values())
        if category:
            templates = [t for t in templates if t.category == category]
        if not include_deprecated:
            templates = [t for t in templates if not t.is_deprecated]
        templates.sort(key=lambda t: (t.id, t.created_at), reverse=True)
        return tuple(templates)

    def insert_preview_environment(self, preview: PreviewEnvironmentRecord) -> None:
        self._state.preview_environments[preview.id] = preview

    def update_preview_environment_status(
        self, preview_id: str, status: str, destroyed_at: datetime | None = None
    ) -> PreviewEnvironmentRecord | None:
        current = self._state.preview_environments.get(preview_id)
        if not current:
            return None
        updated = replace(
            current,
            status=status,
            destroyed_at=destroyed_at if destroyed_at is not None else current.destroyed_at,
        )
        self._state.preview_environments[preview_id] = updated
        return updated

    def preview_environment(self, preview_id: str) -> PreviewEnvironmentRecord | None:
        return self._state.preview_environments.get(preview_id)

    def list_preview_environments(
        self, application_id: UUID | None = None, status: str | None = None
    ) -> tuple[PreviewEnvironmentRecord, ...]:
        previews = list(self._state.preview_environments.values())
        if application_id:
            previews = [p for p in previews if p.application_id == application_id]
        if status:
            previews = [p for p in previews if p.status == status]
        previews.sort(key=lambda p: p.created_at, reverse=True)
        return tuple(previews)

    def expired_preview_environments(self, now: datetime) -> tuple[PreviewEnvironmentRecord, ...]:
        expired = [
            p
            for p in self._state.preview_environments.values()
            if p.status == "active" and p.expires_at <= now
        ]
        return tuple(expired)

    def insert_resource_request(self, request: ResourceRequestRecord) -> None:
        self._state.resource_requests[request.id] = request

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
        current = self._state.resource_requests.get(request_id)
        if not current:
            return None
        updated = replace(
            current,
            status=status,
            status_reason=status_reason if status_reason is not None else current.status_reason,
            provider=provider if provider is not None else current.provider,
            outputs=outputs if outputs is not None else current.outputs,
            approved_by=approved_by if approved_by is not None else current.approved_by,
            updated_at=datetime.now(timezone.utc),
        )
        self._state.resource_requests[request_id] = updated
        return updated

    def resource_request(self, request_id: UUID) -> ResourceRequestRecord | None:
        return self._state.resource_requests.get(request_id)

    def list_resource_requests(
        self,
        application_id: UUID | None = None,
        team_id: str | None = None,
        environment: str | None = None,
        status: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> tuple[tuple[ResourceRequestRecord, ...], str | None, bool]:
        records = list(self._state.resource_requests.values())
        if application_id:
            records = [r for r in records if r.application_id == application_id]
        if team_id:
            records = [r for r in records if r.team_id == team_id]
        if environment:
            records = [r for r in records if r.environment == environment]
        if status:
            records = [r for r in records if r.status == status]
        records.sort(key=lambda r: (r.created_at, r.id), reverse=True)
        return self._paginate(records, limit, cursor, lambda r: (r.created_at, r.id))



class InMemoryDatabase:
    def __init__(self) -> None:
        self._state = _State()
        self._lock = threading.RLock()

    def describe(self) -> str:
        return "memory"

    def health(self) -> str:
        return "ok"

    @contextmanager
    def transaction(self):
        with self._lock:
            staged = self._state.copy()
            yield InMemorySession(staged)
            # Reached only when the block returned normally; an exception propagates
            # out of the `with` and leaves `self._state` untouched, which is the
            # rollback a caller injecting a fault mid-operation must be able to observe.
            self._state = staged

    def clear(self) -> None:
        """Drop every record. Local reset and test setup only."""

        with self._lock:
            self._state = _State()
