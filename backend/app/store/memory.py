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

import copy
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from ..domain.models import Application, DeliveryEvent, Deployment, PipelineRun
from ..persistence import (
    AuditRecord,
    ConcurrentModification,
    IdempotencyRow,
    StillReferenced,
    UnitOfWork,
    VersionConflict,
)
from .records import DeploymentLease, ModuleRow, RequestRow, SystemRow, VersionRow


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
    ) -> None:
        current = self._state.requests.get(str(request_id))
        if current is None:
            return
        self._state.requests[str(request_id)] = replace(
            current,
            status=status,
            comment=comment,
            deployment_id=deployment_id if deployment_id is not None else current.deployment_id,
        )


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
