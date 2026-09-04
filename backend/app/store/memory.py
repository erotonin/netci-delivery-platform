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

from ..domain.models import (
    Application,
    ConfigRevisionStatus,
    DeliveryEvent,
    Deployment,
    ModuleConfigRevision,
    NotificationRecord,
    NotificationStatus,
    PipelineRun,
    PipelineStage,
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
    BreakGlassRecord,
    DeploymentLease,
    ModuleRow,
    PolicyDecisionRecord,
    RequestRow,
    ResourceQuotaRecord,
    SecurityExceptionRecord,
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
