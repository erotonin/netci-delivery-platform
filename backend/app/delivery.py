"""Application-layer module for the netCI delivery reference implementation.

The domain owns state transitions, invariants, idempotency and the source events the
DORA projection reads.  Everything that touches the outside world sits behind a seam:

* `CiLauncher`   -- who actually runs CI (nothing, or a real Jenkins controller)
* `CdOrchestrator` -- who actually runs the long CD process (nothing, or Temporal)
* `PlatformDatabase` -- where state, events, audit and logs are durably written

Every command reads the rows it is about to change inside its own transaction and writes
them back with the version it read, so two replicas racing the same aggregate produce one
winner and one `409`, and a replica that loses the race is correct again on its next
request rather than at its next restart. No state is cached between requests.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Callable
from uuid import UUID, uuid4

from .adapters.cd_orchestrator import (
    CdOrchestrator,
    CdStartError,
    CdStartRequest,
    NullCdOrchestrator,
    PreviewStartRequest,
)
from .adapters.ci_launcher import CiLaunchError, CiLaunchRequest, CiLauncher, NullCiLauncher
from .catalog.previews import PreviewRequest
from .errors import ApiError
from .persistence import (
    AuditRecord,
    ConcurrentModification,
    IdempotencyRow,
    UnitOfWork,
)
from .store import DeploymentLease, PlatformDatabase, PlatformSession, build_database, join
from . import workload_identity
from .stage_catalog import (
    BUILTIN_STAGES,
    StageCatalogError,
    custom_stage,
    custom_stage_parameters,
    resolve_pipeline_stages,
    stage_json,
    validate_stage_parameters,
)
from .runtime_environment import is_local_runtime
from .policy.rules import PolicyDecision, evaluate_artifact_evidence
from .domain.delivery_rules import pull_request_ref
from .domain.freezes import applicable_freeze
from .policy.quota import QuotaEnforcer, QuotaViolation
from .domain.models import (
    Application,
    StageDefinition,
    can_transition_deployment,
    can_transition_pipeline,
    DeliveryEvent,
    DeliveryEventType,
    Deployment,
    DeploymentStatus,
    Environment,
    NotificationRecord,
    PipelineRun,
    PipelineStage,
    PipelineStatus,
    Runtime,
    ScmCommitStatus,
)


logger = logging.getLogger(__name__)

IMMUTABLE_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")

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
        ("checkout", "unit-test", "build", "sbom", "vulnerability-scan", "sign", "publish", "deploy", "health-check"),
    ),
}


class DeliveryError(ApiError):
    """A delivery-domain refusal: the client gets this code and status."""


@dataclass(frozen=True)
class CiResult:
    pipeline_run: PipelineRun
    deployment: Deployment | None = None


def _pr_comment_body(run: PipelineRun, status: ScmCommitStatus, target_url: str | None) -> str:
    """What a pull request is told about its build: what happened, and what it was allowed to do."""

    outcome = "passed" if status == ScmCommitStatus.SUCCESS else "failed"
    if not run.publish_artifact:
        intent = "verify only -- not signed or published (pull request from a fork)"
    elif run.artifact_digest:
        intent = f"published `{run.artifact_digest}`; not deployed -- promote it from netCI"
    else:
        intent = "not published"
    lines = [f"**netCI build {outcome}** for `{run.commit_sha[:12]}`", "", f"- {intent}"]
    if target_url:
        lines.append(f"- [build log]({target_url})")
    lines.append(f"- netCI run `{run.id}`")
    return "\n".join(lines)


@dataclass(frozen=True)
class _CiOutcome:
    """What a CI result produced, and whether a workflow still has to be started.

    Splitting the two lets the whole state change commit inside one transaction while the
    Temporal call that follows happens outside it -- holding a database connection open
    across a network call to another system is how a slow orchestrator becomes a
    connection-pool outage.
    """

    result: CiResult
    pending_cd: tuple[Application, PipelineRun, Deployment] | None = None
    #: What a pull-request build's success asked to have started, once this transaction
    #: commits -- built by `preview_hook`, started by `start_preview`.
    pending_preview: PreviewRequest | None = None
    # A policy denial has to be recorded *and* rejected. Raising inside the transaction
    # would roll back the failed run and the denial audit record along with it, leaving
    # the run stuck at "running" with no explanation -- so the error is carried out of
    # the transaction and raised once the evidence of it is durable.
    deferred_error: "DeliveryError | None" = None


def _now() -> datetime:
    return datetime.now(timezone.utc)


class DeliveryPlatform:
    """Own delivery state and rules behind one application-layer interface."""

    def __init__(
        self,
        ci_launcher: CiLauncher | None = None,
        cd_orchestrator: CdOrchestrator | None = None,
        database: PlatformDatabase | None = None,
    ) -> None:
        if not is_local_runtime() and not self.security_evidence_required():
            raise RuntimeError(
                "NETCI_REQUIRE_SECURITY_EVIDENCE=true is required outside local mode"
            )
        self.ci_launcher: CiLauncher = ci_launcher or NullCiLauncher()
        self.cd_orchestrator: CdOrchestrator = cd_orchestrator or NullCdOrchestrator()
        # Composition builds the seam and stops. Reading state here is what made a second
        # replica answer from a snapshot of its own start-up.
        self.database: PlatformDatabase = database if database is not None else build_database()
        #: Called inside the transaction that records a published build carrying a
        #: release tag. The Portal owns versions and sets this; nothing else does.
        self.build_published_hook: Callable[[PlatformSession, PipelineRun], None] | None = None
        #: Called inside the transaction that records a published build's success, for a
        #: run that may be a pull request's. The Portal owns previews and sets this; it
        #: returns what must be started once the transaction commits, or None when this
        #: run gets no preview (not a pull request, not published, previews not enabled).
        self.preview_hook: Callable[[PlatformSession, PipelineRun], "PreviewRequest | None"] | None = None

    # ------------------------------------------------------------- persistence

    def transaction(self):
        """One transaction a caller can compose several commands into.

        Used by module onboarding, which has to write a delivery application and a Portal
        module together. Storage failures arrive as `DeliveryError`, so composing commands
        does not lose the fail-closed behaviour each of them has on its own.
        """

        return self._transaction()

    @contextmanager
    def _transaction(self, session: PlatformSession | None = None):
        """Open a transaction and turn a storage failure into an answer, not a stack trace.

        Reads go through here too. Now that the database is canonical at request time, an
        unreachable database has to surface on a GET as 503 -- the alternative is a 500
        that tells an operator nothing, or worse, an answer served from a stale snapshot.
        """

        try:
            with join(self.database, session) as transaction:
                yield transaction
        except ConcurrentModification as exc:
            raise DeliveryError(
                "CONCURRENT_MODIFICATION",
                f"record changed while this request was in flight: {exc}",
                409,
            ) from exc
        except (ApiError, KeyError, ValueError):
            # Already a decided answer -- a domain refusal, or one raised by another
            # command composed into this transaction. Reporting it as a storage failure
            # would tell an operator the database is down when a system id was wrong.
            raise
        except Exception as exc:
            raise DeliveryError(
                "PERSISTENCE_UNAVAILABLE", f"cannot reach delivery state: {exc}", 503
            ) from exc

    @staticmethod
    def _require_pipeline_transition(run: PipelineRun, target: PipelineStatus) -> None:
        """Refuse a transition the state machine does not allow.

        `PIPELINE_TRANSITIONS` used to be a table nothing consulted, so the rules lived
        twice -- once in the table and once as scattered `if` statements -- and the two
        drifted: `QUEUED -> SUCCEEDED` was added to the table, which would let a run that
        never started report a successful build. Reading the table here is what makes it
        load-bearing, so a future edit to it changes behaviour instead of documentation.
        """

        if run.status == target:
            return
        if not can_transition_pipeline(run.status, target):
            raise DeliveryError(
                "INVALID_PIPELINE_STATE",
                f"a pipeline run cannot go from {run.status.value} to {target.value}",
                409,
            )

    @staticmethod
    def _require_deployment_transition(
        deployment: Deployment, target: DeploymentStatus
    ) -> None:
        """The same guard for deployments, read from `DEPLOYMENT_TRANSITIONS`."""

        if deployment.status == target:
            return
        if not can_transition_deployment(deployment.status, target):
            raise DeliveryError(
                "INVALID_DEPLOYMENT_STATE",
                f"a deployment cannot go from {deployment.status.value} to {target.value}",
                409,
            )

    def _audit_refusal(self, records: list[AuditRecord]) -> None:
        """Record why a request was refused, in a transaction of its own.

        The refusal itself raises, which rolls back the transaction the caller was in.
        Writing the explanation there would roll it back too, and a refusal nobody can
        explain afterwards is the thing an operator needs most during an incident.
        """

        unit = UnitOfWork(audit=list(records))
        try:
            with self.database.transaction() as transaction:
                transaction.apply(unit)
        except Exception:  # noqa: BLE001 - never let auditing mask the refusal
            logger.exception("could not record the audit trail for a refused request")

    def _apply(self, session: PlatformSession, unit: UnitOfWork) -> None:
        """Write one unit of work, translating storage failures into API answers."""

        try:
            session.apply(unit)
        except ConcurrentModification as exc:
            raise DeliveryError(
                "CONCURRENT_MODIFICATION",
                f"record changed while this request was in flight: {exc}",
                409,
            ) from exc
        except DeliveryError:
            raise
        except Exception as exc:
            raise DeliveryError(
                "PERSISTENCE_UNAVAILABLE", f"cannot persist delivery state: {exc}", 503
            ) from exc

    def _commit(self, unit: UnitOfWork, session: PlatformSession | None = None) -> None:
        """Apply a unit of work in its own transaction, or in the caller's."""

        with self._transaction(session) as transaction:
            self._apply(transaction, unit)

    def persistence_health(self) -> str:
        return self.database.health()

    # ------------------------------------------------------------------- leases

    @staticmethod
    def lease_ttl_seconds() -> int:
        """How long a deployment may hold its target without a heartbeat.

        Long enough that an ordinary deployment never loses its lease mid-flight, short
        enough that a worker which died does not block the target until someone notices.
        The workflow heartbeats, so this bounds *silence*, not duration.
        """

        try:
            return max(30, int(os.getenv("NETCI_DEPLOYMENT_LEASE_TTL_SECONDS", "900")))
        except ValueError:
            return 900

    @staticmethod
    def _digest_in_service(
        transaction: PlatformSession, application_id: UUID, environment: Environment
    ) -> str | None:
        """What this environment is running right now, as far as netCI has established.

        The most recent deployment that ended with a release serving traffic: `healthy`,
        or `rolled_back` (which serves the digest it restored). Recorded on every new
        deployment as `previous_artifact_digest`, so "roll back to what was there" is a
        fact the platform holds rather than something an operator reconstructs from logs.
        """

        # Indexed (migration 0027). This used to load every deployment the application
        # ever had and pick the newest settled one here, on every deployment netCI
        # creates -- so each release of a long-lived service read every release before it.
        return transaction.digest_in_service(application_id, environment.value)

    @staticmethod
    def lease_target(deployment: Deployment, run: PipelineRun | None) -> str:
        """The logical thing a deployment writes to.

        Taking the target from the run's server-managed parameters -- which the Portal
        computed from the module's registered configuration, and which a caller cannot
        supply -- means the lock is over the real resource rather than over a name
        someone chose.

        A host-based deployment locks every host-based deployment of the same application
        in the same environment, not just one that names the identical host set. The key
        used to be the sorted host list, which only ever caught *identical* sets: a module
        whose configuration changed between two releases -- a host added, one retired --
        produced runs with overlapping-but-different lists, so both were granted a lease
        and both wrote to the host that appeared in both. That is exactly the failure
        migration 0010 was written to stop.

        One key per application+environment is coarser than per-host locking and is the
        right invariant here, because netCI never puts two concurrent host deployments of
        one application in one environment on purpose: a rolling release covers all of
        that environment's registered servers, and the strategies that do deploy
        alongside a running release -- canary and blue/green -- are Kubernetes-only and
        key on the namespace below. The host list still reaches the audit record.
        """

        parameters = dict(run.parameters) if run else {}
        namespace = str(parameters.get("target_namespace") or "").strip()
        if namespace:
            return f"namespace:{namespace}"
        hosts = parameters.get("target_hosts")
        if isinstance(hosts, (list, tuple)) and hosts:
            return f"hosts:{deployment.application_id}"
        # No registered target: the whole application in this environment is the resource.
        return f"application:{deployment.application_id}"

    @staticmethod
    def _lease_hosts(run: PipelineRun | None) -> list[str]:
        """The hosts a lease covers, for the audit record the key no longer carries."""

        hosts = (dict(run.parameters) if run else {}).get("target_hosts")
        if isinstance(hosts, (list, tuple)):
            return sorted(str(item) for item in hosts)
        return []

    def _acquire_lease(
        self,
        transaction: PlatformSession,
        unit: UnitOfWork,
        deployment: Deployment,
        run: PipelineRun | None,
        owner: str,
    ) -> DeploymentLease:
        """Take the target, or refuse the deployment. Never proceed without it."""

        target = self.lease_target(deployment, run)
        now = _now()
        lease = transaction.acquire_deployment_lease(
            application_id=deployment.application_id,
            environment=deployment.environment.value,
            target=target,
            deployment_id=deployment.id,
            owner=owner,
            ttl_seconds=self.lease_ttl_seconds(),
            now=now,
        )
        if lease is None:
            holder = transaction.active_deployment_lease(
                application_id=deployment.application_id,
                environment=deployment.environment.value,
                target=target,
            )
            self._audit_refusal(
                [
                    AuditRecord(
                        "deployment.lease_conflict",
                        application_id=deployment.application_id,
                        pipeline_run_id=deployment.pipeline_run_id,
                        deployment_id=deployment.id,
                        payload={
                            "target": target,
                            "environment": deployment.environment.value,
                            "heldBy": holder.owner if holder else "unknown",
                            "heldForDeploymentId": str(holder.deployment_id) if holder else None,
                            "expiresAt": holder.expires_at.isoformat() if holder else None,
                        },
                    )
                ]
            )
            raise DeliveryError(
                "DEPLOYMENT_TARGET_BUSY",
                f"another deployment is already running against {target} in "
                f"{deployment.environment.value}",
                409,
            )
        unit.audit.append(
            AuditRecord(
                "deployment.lease_acquired",
                application_id=deployment.application_id,
                pipeline_run_id=deployment.pipeline_run_id,
                deployment_id=deployment.id,
                actor=owner,
                payload={
                    "target": target,
                    "hosts": self._lease_hosts(run),
                    "environment": deployment.environment.value,
                    "fencingToken": lease.fencing_token,
                    "expiresAt": lease.expires_at.isoformat(),
                },
            )
        )
        return lease

    def _release_lease(
        self,
        transaction: PlatformSession,
        unit: UnitOfWork,
        deployment: Deployment,
        reason: str,
    ) -> None:
        """Give the target back so the next deployment does not wait for the expiry."""

        lease = transaction.deployment_lease(deployment.id)
        if lease is None or lease.released_at is not None:
            return
        transaction.release_deployment_lease(lease.id, reason=reason, now=_now())
        unit.audit.append(
            AuditRecord(
                "deployment.lease_released",
                application_id=deployment.application_id,
                pipeline_run_id=deployment.pipeline_run_id,
                deployment_id=deployment.id,
                payload={"target": lease.target, "reason": reason,
                         "fencingToken": lease.fencing_token},
            )
        )

    def _reject_stale_writer(
        self,
        transaction: PlatformSession,
        deployment: Deployment,
        fencing_token: int | None,
        operation: str,
    ) -> None:
        """Refuse a callback from a workflow that no longer owns this deployment.

        Staleness is not "your token is small" -- a workflow always sends the token it was
        started with, and that token matches its own deployment row. It is "a later
        generation now holds this target": if the target has been claimed again since this
        deployment took it, this writer has been superseded and its result would overwrite
        a newer one.

        A supplied token lower than the deployment's own generation is stale too, and
        cheaper to detect. Both checks are needed: the first catches a workflow replaying
        an old token, the second catches one that legitimately holds an old lease.

        When the target has no current holder, nothing has superseded this deployment and
        an idempotent retry is exactly what the callback looks like.
        """

        current = deployment.fencing_token
        if current is None:
            return

        superseded_by: int | None = None
        if fencing_token is not None and int(fencing_token) < current:
            superseded_by = current
        else:
            lease = transaction.deployment_lease(deployment.id)
            if lease is not None:
                holder = transaction.active_deployment_lease(
                    application_id=lease.application_id,
                    environment=lease.environment,
                    target=lease.target,
                )
                if holder is not None and holder.fencing_token > current:
                    superseded_by = holder.fencing_token
        if superseded_by is None:
            return

        self._audit_refusal(
            [
                AuditRecord(
                    "deployment.stale_callback_rejected",
                    application_id=deployment.application_id,
                    pipeline_run_id=deployment.pipeline_run_id,
                    deployment_id=deployment.id,
                    payload={
                        "operation": operation,
                        "suppliedFencingToken": fencing_token,
                        "currentFencingToken": current,
                        "supersededBy": superseded_by,
                    },
                )
            ]
        )
        raise DeliveryError(
            "STALE_WORKFLOW",
            "this deployment has been taken over by a newer workflow: generation "
            f"{superseded_by} now holds its target, this one holds {current}",
            409,
        )

    def heartbeat_deployment(self, deployment_id: UUID, fencing_token: int) -> dict[str, object]:
        """Extend a running deployment's lease. Refused once it has been taken over."""

        with self._transaction() as transaction:
            deployment = transaction.deployment(deployment_id)
            if deployment is None:
                raise DeliveryError("DEPLOYMENT_NOT_FOUND", "deployment not found", 404)
            self._reject_stale_writer(transaction, deployment, fencing_token, "heartbeat")
            lease = transaction.deployment_lease(deployment_id)
            if lease is None or lease.released_at is not None:
                raise DeliveryError(
                    "LEASE_LOST", "this deployment no longer holds its target", 409
                )
            transaction.heartbeat_deployment_lease(
                lease.id, ttl_seconds=self.lease_ttl_seconds(), now=_now()
            )
            return {
                "deploymentId": str(deployment_id),
                "fencingToken": lease.fencing_token,
                "expiresAt": (
                    _now() + timedelta(seconds=self.lease_ttl_seconds())
                ).isoformat(),
            }

    def recover_expired_leases(self, limit: int = 100) -> list[dict[str, object]]:
        """Release leases whose owner stopped heartbeating, and say so in the audit trail.

        This is not what unblocks a target -- `acquire_deployment_lease` reclaims an
        expired lease at the moment somebody wants it, so there is no window in which a
        dead lease blocks work. This exists so an operator can see that a worker died
        without waiting for the next deployment to reveal it.
        """

        recovered: list[dict[str, object]] = []
        with self._transaction() as transaction:
            now = _now()
            unit = UnitOfWork()
            for lease in transaction.expired_deployment_leases(now, limit):
                transaction.release_deployment_lease(lease.id, reason="expired", now=now)
                unit.audit.append(
                    AuditRecord(
                        "deployment.lease_expired",
                        application_id=lease.application_id,
                        deployment_id=lease.deployment_id,
                        payload={
                            "target": lease.target,
                            "environment": lease.environment,
                            "owner": lease.owner,
                            "fencingToken": lease.fencing_token,
                            "lastHeartbeatAt": lease.heartbeat_at.isoformat(),
                        },
                    )
                )
                recovered.append(
                    {
                        "deploymentId": str(lease.deployment_id),
                        "target": lease.target,
                        "environment": lease.environment,
                        "owner": lease.owner,
                        "fencingToken": lease.fencing_token,
                    }
                )
            if not unit.is_empty():
                self._apply(transaction, unit)
        return recovered

    def reset(self) -> None:
        """Clear the local in-memory store; local reset and test setup only."""

        clear = getattr(self.database, "clear", None)
        if clear is None:
            raise RuntimeError("reset() is only available for the in-memory store")
        clear()

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
        with self._transaction() as transaction:
            stages = [stage_json(item) for item in transaction.stage_catalog()]
        return {"stages": stages, "templates": templates}

    def register_custom_stage(self, *, actor: str, requires_approval: bool = True, **fields: object) -> dict[str, object]:
        """Propose a custom stage. It runs code on every agent of every module that
        selects it, so it becomes usable only when a *different* administrator approves
        (`approve_custom_stage`); with separation of duties off it is active at once."""

        try:
            stage = custom_stage(created_by=actor, status="proposed" if requires_approval else "active", **fields)  # type: ignore[arg-type]
        except StageCatalogError as exc:
            raise DeliveryError(exc.code, exc.message, exc.status_code) from exc
        with self._transaction() as transaction:
            existing = transaction.stage_definition(stage.id)
            if existing is not None and existing.kind == "builtin":
                raise DeliveryError("STAGE_ID_RESERVED", f"{stage.id!r} is a built-in stage", 409)
            if existing is not None and existing.status == "active":
                # Editing an approved stage is a new proposal: the script other modules
                # run must not change under them without a second person seeing it.
                users = [a.name for a in transaction.applications() if stage.id in a.stages]
                if users and requires_approval:
                    raise DeliveryError(
                        "STAGE_IN_USE",
                        f"stage {stage.id!r} is used by {', '.join(sorted(users))}; register the change under a new id",
                        409,
                    )
            transaction.upsert_stage_definition(stage)
            self._apply(transaction, UnitOfWork(audit=[AuditRecord(
                "stage_catalog.proposed" if stage.status == "proposed" else "stage_catalog.registered", actor=actor,
                payload={"stageId": stage.id, "script": stage.script, "afterStage": stage.after_stage, "status": stage.status},
            )]))
        return stage_json(stage)

    def approve_custom_stage(self, stage_id: str, *, actor: str, separation_of_duties: bool = True) -> dict[str, object]:
        with self._transaction() as transaction:
            stage = transaction.stage_definition(stage_id)
            if stage is None:
                raise DeliveryError("STAGE_NOT_FOUND", "no such stage", 404)
            if stage.kind != "custom" or stage.status != "proposed":
                raise DeliveryError("INVALID_STAGE_STATE", f"stage {stage_id!r} is {stage.status}, not waiting for approval", 409)
            if separation_of_duties and stage.created_by == actor:
                raise DeliveryError(
                    "SEPARATION_OF_DUTIES",
                    "a custom stage must be approved by an administrator other than the one who proposed it",
                    403,
                )
            approved = replace(stage, status="active", approved_by=actor, updated_at=_now())
            transaction.upsert_stage_definition(approved)
            self._apply(transaction, UnitOfWork(audit=[AuditRecord(
                "stage_catalog.approved", actor=actor, payload={"stageId": stage_id, "proposedBy": stage.created_by},
            )]))
        return stage_json(approved)

    def remove_custom_stage(self, stage_id: str, *, actor: str) -> None:
        with self._transaction() as transaction:
            stage = transaction.stage_definition(stage_id)
            if stage is None:
                raise DeliveryError("STAGE_NOT_FOUND", "no such stage", 404)
            if stage.kind != "custom":
                raise DeliveryError("STAGE_ID_RESERVED", "built-in stages cannot be removed", 409)
            users = [a.name for a in transaction.applications() if stage_id in a.stages]
            if users:
                raise DeliveryError(
                    "STAGE_IN_USE", f"stage {stage_id!r} is used by: {', '.join(sorted(users))}", 409
                )
            transaction.delete_stage_definition(stage_id)
            self._apply(transaction, UnitOfWork(audit=[AuditRecord(
                "stage_catalog.removed", actor=actor, payload={"stageId": stage_id},
            )]))

    def set_application_stages(
        self, application_id: UUID, stages: list[str], *, actor: str,
        stage_parameters: dict[str, dict[str, str]] | None = None,
    ) -> Application:
        """Change which catalog stages an application's pipeline runs.

        Validated the same way as at creation, against the catalog as it is now. Takes
        effect on the next run: a run carries the stage list it was queued with.
        """

        with self._transaction() as transaction:
            application = transaction.application(application_id)
            if application is None:
                raise DeliveryError("APPLICATION_NOT_FOUND", "application not found", 404)
            template = TEMPLATES[application.pipeline_template]
            catalog = {item.id: item for item in transaction.stage_catalog()}
            resolved = self._validate_stages(template, stages, catalog)
            try:
                values = validate_stage_parameters(resolved, stage_parameters, catalog)
            except StageCatalogError as exc:
                raise DeliveryError(exc.code, exc.message, exc.status_code) from exc
            updated = replace(application, stages=resolved, stage_parameters=values)
            unit = UnitOfWork(applications=[updated])
            unit.audit.append(AuditRecord(
                "application.stages_updated", application_id=application.id, actor=actor,
                payload={"before": list(application.stages), "after": list(resolved), "parameters": values},
            ))
            self._apply(transaction, unit)
        return updated

    def set_application_owner(self, application_id: UUID, owner_team: str | None, *, actor: str) -> Application:
        """Hand an application to another team.

        Ownership decides who may see and change the module, so the transfer is
        audited with both teams named. Delivery history stays with the application.
        """

        with self._transaction() as transaction:
            application = transaction.application(application_id)
            if application is None:
                raise DeliveryError("APPLICATION_NOT_FOUND", "application not found", 404)
            updated = replace(application, owner_team=owner_team)
            unit = UnitOfWork(applications=[updated])
            unit.audit.append(AuditRecord(
                "application.owner_changed", application_id=application.id, actor=actor,
                payload={"before": application.owner_team, "after": owner_team},
            ))
            self._apply(transaction, unit)
        return updated

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
        owner_team: str | None = None,
        session: PlatformSession | None = None,
    ) -> Application:
        """Register an application.

        `session` lets a larger operation -- module onboarding -- create the application
        and its Portal module in one transaction, so a failure after this returns cannot
        leave an application no module points at.
        """

        request_payload = {
            "name": name,
            "repositoryUrl": repository_url,
            "pipelineTemplate": pipeline_template,
            "runtime": runtime.value,
            "defaultEnvironment": default_environment.value,
            "stages": stages,
        }
        # Added to the hash only when it is set. The idempotency record stores a hash of
        # this payload, so adding a field unconditionally rehashes every key that already
        # exists and turns the next replay into IDEMPOTENCY_KEY_REUSED -- an upgrade that
        # breaks in-flight clients. Absent means "as before", which is exactly what an
        # application created before ownership existed sent.
        if owner_team is not None:
            request_payload["ownerTeam"] = owner_team
        with self._transaction(session) as transaction:
            replay = self._idempotent_replay(
                transaction, "application.create", idempotency_key, request_payload
            )
            if replay is not None:
                if not isinstance(replay, Application):
                    raise AssertionError("application idempotency scope returned another result type")
                if replay.name != name:
                    # The key is unique per scope across the whole installation, so a
                    # collision between two applications must be reported rather than
                    # silently answered with someone else's resource.
                    raise DeliveryError(
                        "IDEMPOTENCY_KEY_REUSED", "same key was used with a different request", 409
                    )
                return replay

            template = TEMPLATES.get(pipeline_template)
            if template is None:
                raise DeliveryError("TEMPLATE_NOT_FOUND", "pipeline template does not exist", 422)
            if template.runtime != runtime:
                raise DeliveryError(
                    "RUNTIME_TEMPLATE_MISMATCH", "runtime does not match pipeline template", 422
                )
            selected_stages = self._validate_stages(
                template, stages, {item.id: item for item in transaction.stage_catalog()}
            )
            if transaction.application_by_name(name) is not None:
                raise DeliveryError("APPLICATION_EXISTS", "application name already exists", 409)

            application = self._write_application(
                transaction,
                name=name,
                repository_url=repository_url,
                pipeline_template=pipeline_template,
                runtime=runtime,
                default_environment=default_environment,
                stages=selected_stages,
                owner_team=owner_team,
                idempotency_key=idempotency_key,
                request_payload=request_payload,
            )
        return application

    def _write_application(
        self,
        transaction: PlatformSession,
        *,
        name: str,
        repository_url: str,
        pipeline_template: str,
        runtime: Runtime,
        default_environment: Environment,
        stages: tuple[str, ...],
        owner_team: str | None,
        idempotency_key: str | None,
        request_payload: dict[str, object],
    ) -> Application:
        application = Application(
            name=name,
            repository_url=repository_url,
            pipeline_template=pipeline_template,
            runtime=runtime,
            default_environment=default_environment,
            stages=stages,
            owner_team=owner_team,
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
        self._apply(transaction, unit)
        return application

    def list_applications(self, session: PlatformSession | None = None) -> tuple[Application, ...]:
        with self._transaction(session) as transaction:
            return transaction.applications()

    def get_application(
        self, application_id: UUID, session: PlatformSession | None = None
    ) -> Application:
        with self._transaction(session) as transaction:
            application = transaction.application(application_id)
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
        config_revision_id: UUID | None = None,
        deploy_after_build: bool = True,
        publish_artifact: bool = True,
        release_tag: str | None = None,
        trigger: dict[str, object] | None = None,
    ) -> PipelineRun:
        if deploy_after_build and not publish_artifact:
            # Nothing unpublished has a digest, so there would be nothing to deploy.
            raise DeliveryError("UNPUBLISHED_RUN_CANNOT_DEPLOY",
                                "a run that publishes no artifact cannot be deployed", 422)
        request_payload = {
            "commitSha": commit_sha,
            "branch": branch,
            "environment": environment.value,
            "parameters": parameters,
            "deployAfterBuild": deploy_after_build,
            "publishArtifact": publish_artifact,
            "releaseTag": release_tag,
        }
        # The CI dispatch below is a network call, so it must happen after this
        # transaction commits rather than while it holds a connection open.
        with self._transaction() as transaction:
            application = transaction.application(application_id)
            if application is None:
                raise DeliveryError("APPLICATION_NOT_FOUND", "application not found", 404)
            replay = self._idempotent_replay(
                transaction, "pipeline.start", idempotency_key, request_payload
            )
            if replay is not None:
                if not isinstance(replay, PipelineRun):
                    raise AssertionError("pipeline idempotency scope returned another result type")
                if replay.application_id != application_id:
                    raise DeliveryError(
                        "IDEMPOTENCY_KEY_REUSED", "same key was used with a different request", 409
                    )
                return replay
            self._enforce_pipeline_quota(transaction, application)
            run = self._write_queued_run(
                transaction,
                application_id=application_id,
                commit_sha=commit_sha,
                branch=branch,
                environment=environment,
                parameters=parameters,
                correlation_id=correlation_id,
                idempotency_key=idempotency_key,
                started_by=started_by,
                config_revision_id=config_revision_id,
                request_payload=request_payload,
                deploy_after_build=deploy_after_build,
                publish_artifact=publish_artifact,
                release_tag=release_tag,
                trigger=dict(trigger or {}),
            )
        return self._launch_ci(application, run)

    def _write_queued_run(
        self,
        transaction: PlatformSession,
        *,
        application_id: UUID,
        commit_sha: str,
        branch: str,
        environment: Environment,
        parameters: dict[str, object],
        correlation_id: str,
        idempotency_key: str | None,
        started_by: str | None,
        request_payload: dict[str, object],
        config_revision_id: UUID | None = None,
        deploy_after_build: bool = True,
        publish_artifact: bool = True,
        release_tag: str | None = None,
        trigger: dict[str, object] | None = None,
    ) -> PipelineRun:
        run = PipelineRun(
            application_id=application_id,
            commit_sha=commit_sha,
            branch=branch,
            environment=environment,
            parameters=dict(parameters),
            correlation_id=correlation_id,
            started_by=started_by,
            # The run is pinned to the configuration revision that was active when it was
            # queued. Editing the module afterwards produces a new revision and does not
            # touch this run: a build must deploy to the targets it was approved for, not
            # to whatever the module points at by the time the workflow gets there.
            config_revision_id=config_revision_id,
            deploy_after_build=deploy_after_build,
            publish_artifact=publish_artifact,
            release_tag=release_tag,
            trigger=dict(trigger or {}),
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
        self._notify_scm_status(
            transaction,
            application_id,
            commit_sha,
            ScmCommitStatus.PENDING,
            pipeline_run_id=run.id,
            correlation_id=correlation_id,
            unit=unit,
        )
        unit.notifications.append(
            NotificationRecord(
                id=uuid4(),
                event_type="pipeline.started",
                aggregate_type="pipeline_run",
                aggregate_id=str(run.id),
                payload={
                    "application_id": str(application_id),
                    "commit_sha": commit_sha,
                    "branch": branch,
                    "environment": environment.value,
                    "status": "queued",
                },
                recipient="events@netci.local",
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
        self._apply(transaction, unit)
        return run

    def _custom_stages_for(self, application: Application) -> list[dict[str, object]]:
        with self._transaction() as transaction:
            catalog = {item.id: item for item in transaction.stage_catalog()}
        return custom_stage_parameters(application.stages, catalog, application.stage_parameters)

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
            custom_stages=self._custom_stages_for(application),
            publish_artifact=run.publish_artifact,
            source_ref=pull_request_ref(run.trigger.get("ref")),
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
            console_url=launched.console_url,
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
                payload={
                    "controllerId": launched.controller_id,
                    "externalRunId": launched.external_run_id,
                    "consoleUrl": launched.console_url,
                },
            )
        )
        self._commit(unit)
        return updated

    def _notify_scm_status(
        self,
        session: PlatformSession,
        application_id: UUID,
        commit_sha: str,
        status: ScmCommitStatus,
        *,
        pipeline_run_id: UUID | None = None,
        target_url: str | None = None,
        correlation_id: str | None = None,
        unit: UnitOfWork | None = None,
    ) -> None:
        integration = session.scm_integration_for_application(application_id)
        if integration is None or not integration.enabled:
            return
        # Queued in the same unit of work, sent by the outbox (ADR-048). The status used to
        # be "sent" by a provider method that made no request at all, and an audit record
        # said `scm.status_updated` regardless -- a trail claiming the SCM was told what it
        # never was. What is recorded here is the intent; the outbox records the delivery.
        pending = unit if unit is not None else UnitOfWork()
        common = {
            "provider": integration.provider.value,
            "repository": integration.repository_identity,
            "credentialReference": integration.credential_reference,
        }
        pending.notifications.append(NotificationRecord(
            id=uuid4(), event_type="scm.commit_status", aggregate_type="pipeline_run",
            aggregate_id=str(pipeline_run_id or commit_sha), recipient="scm",
            payload={**common, "commitSha": commit_sha, "state": status.value, "context": "netci/pipeline",
                     "description": f"netCI pipeline {status.value}", "targetUrl": target_url},
        ))
        # The run as this unit of work will leave it (with its digest), not as stored.
        staged = [r for r, _ in pending.runs if pipeline_run_id and r.id == pipeline_run_id]
        run = staged[-1] if staged else (session.pipeline_run(pipeline_run_id) if pipeline_run_id else None)
        number = (run.trigger or {}).get("pullRequest") if run is not None else None
        if run is not None and number and status in {ScmCommitStatus.SUCCESS, ScmCommitStatus.FAILURE}:
            pending.notifications.append(NotificationRecord(
                id=uuid4(), event_type="scm.pr_comment", aggregate_type="pipeline_run",
                aggregate_id=str(run.id), recipient="scm",
                payload={**common, "pullRequest": int(number), "body": _pr_comment_body(run, status, target_url)},
            ))
        pending.audit.append(AuditRecord(
            "scm.status_queued", application_id=application_id, pipeline_run_id=pipeline_run_id,
            correlation_id=correlation_id,
            payload={"provider": integration.provider.value, "status": status.value, "commitSha": commit_sha,
                     "pullRequestComment": bool(number and status in {ScmCommitStatus.SUCCESS, ScmCommitStatus.FAILURE})},
        ))
        if unit is None:
            session.apply(pending)

    def list_pipeline_runs(
        self, application_id: UUID | None = None, session: PlatformSession | None = None
    ) -> tuple[PipelineRun, ...]:
        with self._transaction(session) as transaction:
            return transaction.pipeline_runs(application_id)

    def list_deployments(
        self, application_id: UUID | None = None, session: PlatformSession | None = None
    ) -> tuple[Deployment, ...]:
        with self._transaction(session) as transaction:
            return transaction.deployments(application_id)

    def delivery_events(
        self, application_id: UUID | None = None, session: PlatformSession | None = None
    ) -> tuple[DeliveryEvent, ...]:
        """The only source the DORA projection is allowed to read."""

        with self._transaction(session) as transaction:
            return transaction.delivery_events(application_id)

    def get_pipeline(
        self, pipeline_run_id: UUID, session: PlatformSession | None = None
    ) -> PipelineRun:
        with self._transaction(session) as transaction:
            run = transaction.pipeline_run(pipeline_run_id)
        if run is None:
            raise DeliveryError("PIPELINE_NOT_FOUND", "pipeline run not found", 404)
        return run

    def get_pipeline_logs(self, pipeline_run_id: UUID) -> tuple[PipelineRun, tuple[str, ...]]:
        with self._transaction() as transaction:
            run = transaction.pipeline_run(pipeline_run_id)
            if run is None:
                raise DeliveryError("PIPELINE_NOT_FOUND", "pipeline run not found", 404)
            return run, transaction.pipeline_logs(pipeline_run_id)

    def append_pipeline_logs(self, pipeline_run_id: UUID, lines: list[str]) -> None:
        """Append log lines to a run.

        Sequence numbers come from a per-run counter reserved with `UPDATE ... RETURNING`,
        not from `max(sequence) + 1`: under concurrency two writers read the same max, both
        insert it, and the loser dies on the primary key taking its whole unit of work --
        the state change it was carrying -- with it.
        """

        if not lines:
            return
        with self._transaction() as transaction:
            if transaction.pipeline_run(pipeline_run_id) is None:
                raise DeliveryError("PIPELINE_NOT_FOUND", "pipeline run not found", 404)
            unit = UnitOfWork()
            unit.logs.append((pipeline_run_id, list(lines)))
            self._apply(transaction, unit)

    def get_deployment(
        self, deployment_id: UUID, session: PlatformSession | None = None
    ) -> Deployment:
        with self._transaction(session) as transaction:
            deployment = transaction.deployment(deployment_id)
        if deployment is None:
            raise DeliveryError("DEPLOYMENT_NOT_FOUND", "deployment not found", 404)
        return deployment

    def cancel_pipeline(self, pipeline_run_id: UUID, actor: str, reason: str = "") -> PipelineRun:
        with self._transaction() as transaction:
            run = transaction.pipeline_run(pipeline_run_id)
            if run is None:
                raise DeliveryError("PIPELINE_NOT_FOUND", "pipeline run not found", 404)
            if run.status == PipelineStatus.CANCELLED:
                return run
            if run.status in {PipelineStatus.SUCCEEDED, PipelineStatus.FAILED, PipelineStatus.ROLLED_BACK}:
                raise DeliveryError("INVALID_PIPELINE_STATE", f"cannot cancel a pipeline run with status {run.status.value}", 409)

            now = _now()
            updated_run = replace(run, status=PipelineStatus.CANCELLED, version=run.version + 1, updated_at=now)
            unit = UnitOfWork(runs=[(updated_run, run.version)])
            unit.logs.append((run.id, [f"run cancelled by {actor}: {reason}".strip()]))
            unit.audit.append(
                AuditRecord(
                    "pipeline.cancelled",
                    application_id=run.application_id,
                    pipeline_run_id=run.id,
                    actor=actor,
                    correlation_id=run.correlation_id,
                    payload={"reason": reason},
                )
            )
            self._notify_scm_status(
                transaction,
                run.application_id,
                run.commit_sha,
                ScmCommitStatus.CANCELLED,
                pipeline_run_id=run.id,
                correlation_id=run.correlation_id,
                unit=unit,
            )
            # Also cancel active deployments for this run
            deployments_to_cancel = [
                d for d in transaction.deployments(pipeline_run_id=run.id)
                if d.status in {DeploymentStatus.PENDING_APPROVAL, DeploymentStatus.DEPLOYING}
            ]
            for dep in deployments_to_cancel:
                cancelled_dep = replace(dep, status=DeploymentStatus.CANCELLED, version=dep.version + 1, updated_at=now)
                unit.deployments.append((cancelled_dep, dep.version))
                self._release_lease(transaction, unit, cancelled_dep, reason="cancelled")
                unit.audit.append(
                    AuditRecord(
                        "deployment.cancelled",
                        application_id=dep.application_id,
                        pipeline_run_id=run.id,
                        deployment_id=dep.id,
                        actor=actor,
                        payload={"reason": reason},
                    )
                )
            self._apply(transaction, unit)

        target_ci_id = run.jenkins_run_id or str(run.id)
        if target_ci_id:
            try:
                self.ci_launcher.abort(target_ci_id)
            except Exception as exc:
                logger.warning("failed to abort Jenkins run %s: %s", target_ci_id, exc)

        if run.workflow_id:
            try:
                self.cd_orchestrator.cancel(run.workflow_id)
            except Exception as exc:
                logger.warning("failed to cancel CD workflow %s: %s", run.workflow_id, exc)

        return updated_run

    def cancel_deployment(self, deployment_id: UUID, actor: str, reason: str = "") -> Deployment:
        with self._transaction() as transaction:
            dep = transaction.deployment(deployment_id)
            if dep is None:
                raise DeliveryError("DEPLOYMENT_NOT_FOUND", "deployment not found", 404)
            if dep.status == DeploymentStatus.CANCELLED:
                return dep
            if dep.status in {
                DeploymentStatus.HEALTHY,
                DeploymentStatus.FAILED,
                DeploymentStatus.ROLLED_BACK,
                DeploymentStatus.ROLLBACK_FAILED,
            }:
                raise DeliveryError("INVALID_DEPLOYMENT_STATE", f"cannot cancel a deployment with status {dep.status.value}", 409)

            now = _now()
            updated_dep = replace(dep, status=DeploymentStatus.CANCELLED, version=dep.version + 1, updated_at=now)
            unit = UnitOfWork(deployments=[(updated_dep, dep.version)])
            self._release_lease(transaction, unit, updated_dep, reason="cancelled")
            unit.audit.append(
                AuditRecord(
                    "deployment.cancelled",
                    application_id=dep.application_id,
                    pipeline_run_id=dep.pipeline_run_id,
                    deployment_id=dep.id,
                    actor=actor,
                    payload={"reason": reason},
                )
            )
            run = transaction.pipeline_run(dep.pipeline_run_id) if dep.pipeline_run_id else None
            if run and run.status in {PipelineStatus.RUNNING, PipelineStatus.WAITING_APPROVAL}:
                updated_run = replace(run, status=PipelineStatus.CANCELLED, version=run.version + 1, updated_at=now)
                unit.runs.append((updated_run, run.version))
                unit.logs.append((run.id, [f"deployment cancelled by {actor}: {reason}".strip()]))
                unit.audit.append(
                    AuditRecord(
                        "pipeline.cancelled",
                        application_id=run.application_id,
                        pipeline_run_id=run.id,
                        actor=actor,
                        correlation_id=run.correlation_id,
                        payload={"reason": reason},
                    )
                )
                self._notify_scm_status(
                    transaction,
                    run.application_id,
                    run.commit_sha,
                    ScmCommitStatus.CANCELLED,
                    pipeline_run_id=run.id,
                    correlation_id=run.correlation_id,
                    unit=unit,
                )
            self._apply(transaction, unit)

        workflow_id = run.workflow_id if run and run.workflow_id else f"netci-deploy-{dep.id}"
        try:
            self.cd_orchestrator.cancel(workflow_id)
        except Exception as exc:
            logger.warning("failed to cancel CD workflow %s: %s", workflow_id, exc)

        return updated_dep

    def retry_pipeline(
        self,
        pipeline_run_id: UUID,
        actor: str,
        idempotency_key: str | None = None,
        parameters: dict[str, object] | None = None,
        config_revision_id: UUID | None = None,
    ) -> PipelineRun:
        """Queue a new run for the parent's commit.

        `parameters` and `config_revision_id` are the module's *current* desired target
        configuration, resolved by the caller: a retry happens now, so it must deploy
        what is active now. Copying the parent's binding would replay a configuration
        that may since have been superseded -- and a parent with no revision pinned
        would leave the retry unpinned, which the drift check then reported as a running
        deployment on "revision none".
        """

        request_payload = {"parentRunId": str(pipeline_run_id)}
        with self._transaction() as transaction:
            parent = transaction.pipeline_run(pipeline_run_id)
            if parent is None:
                raise DeliveryError("PIPELINE_NOT_FOUND", "pipeline run not found", 404)
            if parent.status in {PipelineStatus.QUEUED, PipelineStatus.RUNNING, PipelineStatus.WAITING_APPROVAL}:
                raise DeliveryError("RUN_STILL_ACTIVE", "cannot retry an active pipeline run", 409)

            application = transaction.application(parent.application_id)
            if application is None:
                raise DeliveryError("APPLICATION_NOT_FOUND", "application not found", 404)

            replay = self._idempotent_replay(
                transaction, "pipeline.retry", idempotency_key, request_payload
            )
            if replay is not None:
                if not isinstance(replay, PipelineRun):
                    raise AssertionError("pipeline retry idempotency scope returned another result type")
                return replay

            application_for_quota = transaction.application(parent.application_id)
            if application_for_quota is not None:
                self._enforce_pipeline_quota(transaction, application_for_quota)
            corr_id = f"retry-{parent.correlation_id or parent.id}-{uuid4().hex[:6]}"
            new_run = PipelineRun(
                application_id=parent.application_id,
                commit_sha=parent.commit_sha,
                branch=parent.branch,
                environment=parent.environment,
                parameters=dict(parameters if parameters is not None else parent.parameters),
                correlation_id=corr_id,
                started_by=actor,
                retry_of=parent.id,
                config_revision_id=config_revision_id if config_revision_id is not None else parent.config_revision_id,
                # A retry is the same intent run again: a fork's verify-only build must not
                # come back from a retry as a published, deployable one.
                deploy_after_build=parent.deploy_after_build,
                publish_artifact=parent.publish_artifact,
                release_tag=parent.release_tag,
                trigger=dict(parent.trigger),
            )
            unit = UnitOfWork(runs=[(new_run, None)])
            unit.logs.append(
                (
                    new_run.id,
                    [
                        f"retried from {parent.id}",
                        f"queued correlationId={corr_id}",
                        f"commit={new_run.commit_sha}",
                        f"startedBy={actor}",
                    ],
                )
            )
            unit.audit.append(
                AuditRecord(
                    "pipeline.retried",
                    application_id=parent.application_id,
                    pipeline_run_id=new_run.id,
                    actor=actor,
                    correlation_id=corr_id,
                    payload={"parentRunId": str(parent.id), "newRunId": str(new_run.id)},
                )
            )
            self._notify_scm_status(
                transaction,
                parent.application_id,
                new_run.commit_sha,
                ScmCommitStatus.PENDING,
                pipeline_run_id=new_run.id,
                correlation_id=corr_id,
                unit=unit,
            )
            if idempotency_key is not None:
                unit.idempotency.append(
                    IdempotencyRow(
                        scope="pipeline.retry",
                        idempotency_key=idempotency_key,
                        request_hash=self._payload_hash(request_payload),
                        resource_type="pipeline_run",
                        resource_id=new_run.id,
                        response_status=201,
                    )
                )
            self._apply(transaction, unit)

        return self._launch_ci(application, new_run)

    def record_stage_event(
        self,
        pipeline_run_id: UUID,
        *,
        stage_id: str,
        stage_name: str,
        status: str,
        attempt: int = 1,
        queued_at: datetime | None = None,
        started_at: datetime | None = None,
        completed_at: datetime | None = None,
        duration_ms: int | None = None,
        error_message: str | None = None,
        log_snippet: str | None = None,
    ) -> PipelineStage:
        with self._transaction() as transaction:
            run = transaction.pipeline_run(pipeline_run_id)
            if run is None:
                raise DeliveryError("PIPELINE_NOT_FOUND", "pipeline run not found", 404)
            stage = PipelineStage(
                pipeline_run_id=pipeline_run_id,
                stage_id=stage_id,
                stage_name=stage_name,
                status=status,
                attempt=attempt,
                queued_at=queued_at,
                started_at=started_at,
                completed_at=completed_at,
                duration_ms=duration_ms,
                error_message=error_message,
                log_snippet=log_snippet,
            )
            saved = transaction.record_pipeline_stage(stage)
            unit = UnitOfWork()
            unit.audit.append(
                AuditRecord(
                    "pipeline.stage",
                    application_id=run.application_id,
                    pipeline_run_id=pipeline_run_id,
                    correlation_id=run.correlation_id,
                    payload={
                        "stageId": stage_id,
                        "stageName": stage_name,
                        "status": status,
                        "attempt": attempt,
                        "durationMs": duration_ms,
                    },
                )
            )
            if log_snippet:
                unit.logs.append((pipeline_run_id, [f"[{stage_name}] {line}" for line in log_snippet.splitlines()]))
            if error_message:
                unit.logs.append((pipeline_run_id, [f"[{stage_name}] ERROR: {error_message}"]))
            self._apply(transaction, unit)
            return saved

    def list_pipeline_stages(self, pipeline_run_id: UUID) -> tuple[PipelineStage, ...]:
        with self._transaction() as transaction:
            run = transaction.pipeline_run(pipeline_run_id)
            if run is None:
                raise DeliveryError("PIPELINE_NOT_FOUND", "pipeline run not found", 404)
            return transaction.pipeline_stages(pipeline_run_id)

    # ------------------------------------------------------------- invariants

    @staticmethod
    def _validate_stages(
        template: TemplateDefinition, stages: list[str], catalog: dict[str, StageDefinition] | None = None
    ) -> tuple[str, ...]:
        """Resolve a module's choice against the catalog (see stage_catalog.py)."""

        known = catalog if catalog is not None else {s.id: s for s in BUILTIN_STAGES}
        try:
            return resolve_pipeline_stages(template.stages, list(stages), known)
        except StageCatalogError as exc:
            raise DeliveryError(exc.code, exc.message, exc.status_code) from exc

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

    @staticmethod
    def _idempotent_replay(
        transaction: PlatformSession,
        scope: str,
        idempotency_key: str | None,
        request_payload: object,
    ) -> Application | PipelineRun | None:
        """Answer a retry from the durable record, not from a process-local memory.

        Reading the record in the caller's transaction is what makes a retry after a
        network timeout return the original resource on any replica and after any
        restart: the row was written in the same transaction as the resource itself,
        so one exists if and only if the other does.
        """

        if idempotency_key is None:
            return None
        record = transaction.idempotency(scope, idempotency_key)
        if record is None:
            return None
        if record.request_hash != DeliveryPlatform._payload_hash(request_payload):
            raise DeliveryError("IDEMPOTENCY_KEY_REUSED", "same key was used with a different request", 409)
        if record.resource_type == "application":
            return transaction.application(record.resource_id)
        if record.resource_type == "pipeline_run":
            # The current state of the run, not a snapshot taken at first use.
            return transaction.pipeline_run(record.resource_id)
        return None

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

        digest = evidence.get("artifactDigest")
        if not isinstance(digest, str) or not IMMUTABLE_DIGEST.fullmatch(digest):
            raise DeliveryError(
                "IMMUTABLE_ARTIFACT_REQUIRED", "security evidence requires a sha256 artifact digest", 422
            )
        with self._transaction() as transaction:
            run = transaction.pipeline_run(pipeline_run_id)
            if run is None:
                raise DeliveryError("PIPELINE_NOT_FOUND", "pipeline run not found", 404)
            stored = {
                **evidence,
                "pipelineRunId": str(run.id),
                "applicationId": str(run.application_id),
            }
            application = transaction.application(run.application_id)
            decision = evaluate_artifact_evidence(
                stored, expected_digest=digest, require_evidence=True,
                # Bound here, where the run is known: the verdict stored below binds every
                # later evaluation, so a mismatch recorded now cannot be re-read as allow.
                expected_source=(application.repository_url if application else "", run.commit_sha),
            )
            stored["decision"] = "allow" if decision.allowed else "deny"
            stored["reason"] = decision.reason
            unit = UnitOfWork()
            unit.security_evidence.append((run.id, run.application_id, digest, stored))
            unit.logs.append(
                (run.id, [f"security-evidence decision={stored['decision']} reason={decision.reason}"])
            )
            unit.audit.append(
                AuditRecord(
                    "artifact.evidence_recorded",
                    application_id=run.application_id,
                    pipeline_run_id=run.id,
                    correlation_id=run.correlation_id,
                    payload=decision.as_json(),
                )
            )
            self._apply(transaction, unit)
        return decision

    def security_evidence(
        self, pipeline_run_id: UUID, session: PlatformSession | None = None
    ) -> dict[str, object]:
        with self._transaction(session) as transaction:
            evidence = transaction.security_evidence(pipeline_run_id)
        if evidence is None:
            raise DeliveryError("EVIDENCE_NOT_FOUND", "no security evidence for this pipeline run", 404)
        return dict(evidence)

    def audit_records(
        self, application_ids: set[UUID] | None = None, session: PlatformSession | None = None
    ) -> tuple[AuditRecord, ...]:
        """Return the immutable audit ledger, optionally scoped to applications.

        Portal code consumes this interface instead of reconstructing audit facts from
        mutable run state. That preserves the actor, correlation id and exact event type
        written at decision time.
        """

        with self._transaction(session) as transaction:
            return transaction.audit_records(application_ids)

    def _enforce_artifact_policy(
        self, transaction: PlatformSession, run: PipelineRun, artifact_digest: str
    ) -> PolicyDecision:
        """Refuse to move an artifact that cannot prove where it came from."""

        decision = evaluate_artifact_evidence(
            transaction.security_evidence(run.id),
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
        self._apply(transaction, unit)
        return decision

    # ------------------------------------------------------------- CI results

    def record_ci_result(
        self,
        pipeline_run_id: UUID,
        result_status: str,
        artifact_digest: str | None,
        log_lines: list[str],
    ) -> CiResult:
        with self._transaction() as transaction:
            outcome = self._record_ci_result(transaction, pipeline_run_id, result_status,
                                             artifact_digest, log_lines)
        if outcome.deferred_error is not None:
            raise outcome.deferred_error
        if outcome.pending_preview is not None:
            # Also a network call outside the transaction above -- see _start_cd.
            self.start_preview(outcome.pending_preview)
        if outcome.pending_cd is None:
            return outcome.result
        # Starting the CD workflow is a network call to Temporal and must not run while
        # the transaction above holds a connection open.
        application, run, deployment = outcome.pending_cd
        started = self._start_cd(application, run, deployment)
        return CiResult(self.get_pipeline(run.id), started)

    def _record_ci_result(
        self,
        transaction: PlatformSession,
        pipeline_run_id: UUID,
        result_status: str,
        artifact_digest: str | None,
        log_lines: list[str],
    ) -> "_CiOutcome":
        run = transaction.pipeline_run(pipeline_run_id)
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
            self._notify_scm_status(
                transaction,
                run.application_id,
                run.commit_sha,
                ScmCommitStatus.RUNNING,
                pipeline_run_id=run.id,
                correlation_id=run.correlation_id,
                unit=unit,
            )
            self._apply(transaction, unit)
            return _CiOutcome(CiResult(updated))

        if result_status not in {PipelineStatus.SUCCEEDED.value, PipelineStatus.FAILED.value}:
            raise DeliveryError("INVALID_CI_RESULT", "CI result status is not supported", 422)
        # The table decides. A queued run may be closed as failed or cancelled, but not
        # reported successful: nothing ran, so there is no success to report. A caller
        # holding a genuine success for a queued run has lost the `running` callback and
        # must record that first -- see `Reconciler._repair_lost_running_transition`.
        self._require_pipeline_transition(run, PipelineStatus(result_status))

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
            self._notify_scm_status(
                transaction,
                run.application_id,
                run.commit_sha,
                ScmCommitStatus.FAILURE,
                pipeline_run_id=run.id,
                correlation_id=run.correlation_id,
                unit=unit,
            )
            unit.notifications.append(
                NotificationRecord(
                    id=uuid4(),
                    event_type="pipeline.completed",
                    aggregate_type="pipeline_run",
                    aggregate_id=str(run.id),
                    payload={
                        "application_id": str(run.application_id),
                        "status": "failed",
                        "commit_sha": run.commit_sha,
                    },
                    recipient="events@netci.local",
                )
            )
            self._apply(transaction, unit)
            return _CiOutcome(CiResult(updated))

        if not run.publish_artifact:
            # A verify-only build (a fork's pull request) was dispatched with Sign and
            # Publish switched off. A digest arriving for it came from the build's own
            # code, which is exactly the code nobody has reviewed: refuse it rather than
            # record something that looks like an artifact.
            if artifact_digest:
                raise DeliveryError(
                    "UNPUBLISHED_RUN_HAS_NO_ARTIFACT",
                    "this run was dispatched verify-only; it publishes no artifact and may not report one",
                    422,
                )
            return self._record_build_success(transaction, run, None, log_lines)

        if not artifact_digest or not IMMUTABLE_DIGEST.fullmatch(artifact_digest):
            raise DeliveryError(
                "IMMUTABLE_ARTIFACT_REQUIRED", "successful CI requires a sha256 artifact digest", 422
            )

        decision = self._enforce_artifact_policy(transaction, run, artifact_digest)
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
            self._notify_scm_status(
                transaction,
                run.application_id,
                run.commit_sha,
                ScmCommitStatus.FAILURE,
                pipeline_run_id=run.id,
                correlation_id=run.correlation_id,
                unit=unit,
            )
            self._apply(transaction, unit)
            return _CiOutcome(
                CiResult(failed),
                deferred_error=DeliveryError("ARTIFACT_POLICY_DENIED", decision.reason, 422),
            )

        if not run.deploy_after_build:
            # Built, verified and published; deploying it is a separate decision -- a
            # promotion, or a production request naming the version it became.
            return self._record_build_success(transaction, run, artifact_digest, log_lines)
        freeze = self._active_freeze(transaction, run.application_id, run.environment)
        if freeze is not None:
            # The build is real and is recorded; only the deployment waits. Refusing the
            # callback instead would lose the build's result. Promote it once the freeze ends.
            note = (f"not deployed: {run.environment.value} is frozen until {freeze.ends_at.isoformat()} "
                    f"({freeze.name}); promote this run after the freeze")
            return self._record_build_success(transaction, run, artifact_digest, [*(log_lines or []), note])

        application = transaction.application(run.application_id)
        if application is None:
            raise DeliveryError("APPLICATION_NOT_FOUND", "application not found", 404)
        requires_approval = run.environment == Environment.PROD
        deployment = Deployment(
            application_id=run.application_id,
            pipeline_run_id=run.id,
            runtime=application.runtime,
            environment=run.environment,
            artifact_digest=artifact_digest,
            previous_artifact_digest=self._digest_in_service(transaction, run.application_id, run.environment),
            # Inherited, not re-read: the deployment must use the configuration the run
            # was queued against, whatever the module says now.
            config_revision_id=run.config_revision_id,
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
        if deployment.status == DeploymentStatus.DEPLOYING:
            self._apply(transaction, UnitOfWork(deployments=[(deployment, None)]))
            # Claim the target before anything is written that says a deployment is
            # under way. If someone else holds it this raises, and nothing is committed.
            lease = self._acquire_lease(
                transaction, unit, deployment, updated, owner=f"ci:{run.id}"
            )
            deployment = replace(deployment, fencing_token=lease.fencing_token)
            unit.deployments = [(deployment, 1)]
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
        self._notify_scm_status(
            transaction,
            run.application_id,
            run.commit_sha,
            ScmCommitStatus.SUCCESS,
            pipeline_run_id=run.id,
            target_url=run.console_url,
            correlation_id=run.correlation_id,
            unit=unit,
        )
        unit.notifications.append(
            NotificationRecord(
                id=uuid4(),
                event_type="deployment.started" if deployment.status == DeploymentStatus.DEPLOYING else "deployment.pending_approval",
                aggregate_type="deployment",
                aggregate_id=str(deployment.id),
                payload={
                    "application_id": str(run.application_id),
                    "environment": run.environment.value,
                    "status": deployment.status.value,
                    "artifact_digest": artifact_digest,
                },
                recipient="events@netci.local",
            )
        )
        self._apply(transaction, unit)
        self._build_published(transaction, updated)

        # A production deployment starts its durable workflow only after approval;
        # anything else can begin immediately, once this transaction has committed.
        if deployment.status != DeploymentStatus.DEPLOYING:
            return _CiOutcome(CiResult(updated, deployment))
        return _CiOutcome(CiResult(updated, deployment), pending_cd=(application, updated, deployment))

    @staticmethod
    def _enforce_pipeline_quota(transaction: PlatformSession, application: Application) -> None:
        try:
            QuotaEnforcer.check_pipeline_quota(
                transaction, application_id=application.id, team=application.owner_team
            )
        except QuotaViolation as exc:
            raise DeliveryError("PIPELINE_QUOTA_EXCEEDED", str(exc), 429) from exc

    def _active_freeze(self, transaction: PlatformSession, application_id: UUID, environment: Environment):
        """The change freeze that stops a deployment starting now, unless a break-glass
        was granted for that very freeze (ADR-047). Rollbacks never ask."""

        now = _now()
        module = transaction.portal_module_for_application(application_id)
        freeze = applicable_freeze(
            transaction.change_freezes(ending_after=now), environment=environment.value,
            system_id=module.system_id if module else None, module_id=module.id if module else None, at=now,
        )
        if freeze is None or transaction.active_break_glass("change_freeze", str(freeze.id), now) is not None:
            return None
        return freeze

    def _refuse_during_freeze(self, transaction: PlatformSession, application_id: UUID, environment: Environment) -> None:
        freeze = self._active_freeze(transaction, application_id, environment)
        if freeze is not None:
            raise DeliveryError(
                "CHANGE_FREEZE",
                f"{environment.value} is frozen until {freeze.ends_at.isoformat()} ({freeze.name}: {freeze.reason}); "
                f"a break-glass on change_freeze/{freeze.id} is the way through",
                409,
            )

    def _record_build_success(
        self,
        transaction: PlatformSession,
        run: PipelineRun,
        artifact_digest: str | None,
        log_lines: list[str] | None,
    ) -> "_CiOutcome":
        """A run whose intent ends at the build: `succeeded`, and no deployment."""

        updated = replace(
            run, status=PipelineStatus.SUCCEEDED, artifact_digest=artifact_digest,
            version=run.version + 1, updated_at=_now(),
        )
        unit = UnitOfWork(runs=[(updated, run.version)])
        if log_lines:
            unit.logs.append((run.id, list(log_lines)))
        unit.logs.append((run.id, [
            "build succeeded; not deployed (" + ("verify only: nothing published" if artifact_digest is None
                                                  else f"artifact {artifact_digest}") + ")"
        ]))
        unit.audit.append(
            AuditRecord(
                "pipeline.succeeded",
                application_id=run.application_id,
                pipeline_run_id=run.id,
                correlation_id=run.correlation_id,
                payload={"artifactDigest": artifact_digest, "deployed": False,
                         "published": run.publish_artifact},
            )
        )
        self._notify_scm_status(
            transaction,
            run.application_id,
            run.commit_sha,
            ScmCommitStatus.SUCCESS,
            pipeline_run_id=run.id,
            target_url=run.console_url,
            correlation_id=run.correlation_id,
            unit=unit,
        )
        unit.notifications.append(
            NotificationRecord(
                id=uuid4(),
                event_type="pipeline.completed",
                aggregate_type="pipeline_run",
                aggregate_id=str(run.id),
                payload={
                    "application_id": str(run.application_id),
                    "status": "succeeded",
                    "commit_sha": run.commit_sha,
                    "artifact_digest": artifact_digest,
                    "deployed": False,
                },
                recipient="events@netci.local",
            )
        )
        self._apply(transaction, unit)
        # The tagged version belongs to this same transaction (ADR-043); the preview is a
        # side effect after it. bf30e27 replaced this call with the preview hook, and a
        # v* tag silently stopped becoming a version.
        self._build_published(transaction, updated)
        preview_request = None
        if self.preview_hook is not None:
            try:
                preview_request = self.preview_hook(transaction, updated)
            except Exception as exc:
                logger.warning("preview_hook failed for run %s: %s", updated.id, exc, exc_info=True)
        return _CiOutcome(CiResult(updated), pending_preview=preview_request)

    def _build_published(self, transaction: PlatformSession, run: PipelineRun) -> None:
        if self.build_published_hook is not None and run.artifact_digest and run.release_tag:
            self.build_published_hook(transaction, run)

    def redeploy_artifact(
        self,
        application_id: UUID,
        *,
        environment: Environment,
        source_pipeline_run_id: UUID,
        config_revision_id: UUID | None,
        actor: str,
        parameters: dict[str, object],
        reason: str,
    ) -> Deployment:
        """Deploy an artifact that already exists -- a configuration change, or a re-roll.

        This is the honest form of "apply configuration without rebuilding". It used to
        create a `Deployment` marked `healthy` directly, with a digest invented when no
        real one existed, and emit a DORA deployment event for it -- a deployment that
        had touched no runtime, reported as live. Here the artifact must be the output of
        a real succeeded run, the deployment goes through the same lease, state machine
        and workflow as any other, and health is whatever the worker reports.
        """

        with self._transaction() as transaction:
            application = transaction.application(application_id)
            if application is None:
                raise DeliveryError("APPLICATION_NOT_FOUND", "application not found", 404)
            source = transaction.pipeline_run(source_pipeline_run_id)
            if source is None or source.application_id != application_id:
                raise DeliveryError("PIPELINE_NOT_FOUND", "source pipeline run not found", 404)
            # A run whose release was later rolled back still built and verified its
            # artifact; the rollback is a fact about a deployment, not about the digest.
            if source.status not in {PipelineStatus.SUCCEEDED, PipelineStatus.RUNNING, PipelineStatus.ROLLED_BACK} or not source.artifact_digest:
                raise DeliveryError(
                    "NO_DEPLOYABLE_ARTIFACT",
                    "the source run has no verified artifact digest; a configuration cannot be "
                    "applied to an artifact that was never built",
                    409,
                )
            evidence = transaction.security_evidence(source.id)
            decision = evaluate_artifact_evidence(
                evidence,
                expected_digest=source.artifact_digest,
                require_evidence=self.security_evidence_required(),
            )
            if not decision.allowed:
                raise DeliveryError("ARTIFACT_POLICY_DENIED", decision.reason, 422)

            requires_approval = environment == Environment.PROD
            if not requires_approval:
                # Prod is checked at approval, when it starts moving.
                self._refuse_during_freeze(transaction, application_id, environment)
            now = _now()
            # The run is what carries the server-managed target for the lease and the
            # workflow. A redeploy reuses the run that built the artifact, with the new
            # configuration's parameters layered on -- the config revision is what changed.
            carrier = replace(
                source,
                parameters={**dict(source.parameters), **dict(parameters)},
                config_revision_id=config_revision_id,
            )
            deployment = Deployment(
                application_id=application_id,
                pipeline_run_id=source.id,
                runtime=application.runtime,
                environment=environment,
                artifact_digest=source.artifact_digest,
                previous_artifact_digest=self._digest_in_service(transaction, application_id, environment),
                config_revision_id=config_revision_id,
                status=(
                    DeploymentStatus.PENDING_APPROVAL
                    if requires_approval
                    else DeploymentStatus.DEPLOYING
                ),
                created_at=now,
                updated_at=now,
            )
            unit = UnitOfWork()
            if deployment.status == DeploymentStatus.DEPLOYING:
                # The lease row references the deployment row (foreign key), so the
                # deployment is written first, inside the same transaction; a refused
                # lease still raises and rolls both back. The in-memory store enforces
                # no such constraint, which is how this order was wrong until the first
                # redeploy against PostgreSQL.
                self._apply(transaction, UnitOfWork(deployments=[(deployment, None)]))
                lease = self._acquire_lease(
                    transaction, unit, deployment, carrier, owner=f"redeploy:{actor}"
                )
                deployment = replace(deployment, fencing_token=lease.fencing_token)
                unit.deployments = [(deployment, 1)]
            else:
                unit.deployments = [(deployment, None)]
            unit.audit.append(
                AuditRecord(
                    "deployment.redeploy_requested",
                    application_id=application_id,
                    pipeline_run_id=source.id,
                    deployment_id=deployment.id,
                    actor=actor,
                    correlation_id=source.correlation_id,
                    payload={
                        "environment": environment.value,
                        "artifactDigest": source.artifact_digest,
                        "configRevisionId": str(config_revision_id) if config_revision_id else None,
                        "reason": reason,
                        "requiresApproval": requires_approval,
                    },
                )
            )
            unit.logs.append((source.id, [f"redeploy requested by={actor} env={environment.value} reason={reason}"]))
            self._apply(transaction, unit)

        if deployment.status == DeploymentStatus.DEPLOYING:
            # Outside the transaction: this reaches Temporal.
            self._start_cd(application, carrier, deployment)
        return self.get_deployment(deployment.id)

    def _workflow_parameters(
        self, application: Application, run: PipelineRun, deployment: Deployment
    ) -> dict[str, object]:
        """What the worker needs, from the run that built the artifact being moved.

        `run` is the run whose evidence locates the artifact: the deployment's own run
        for a deployment, and the run that built the *target* digest for a rollback.
        """

        parameters = dict(run.parameters)
        with self._transaction() as transaction:
            evidence = transaction.security_evidence(run.id) or {}
        artifact_ref = str(evidence.get("artifactRef") or "").strip()
        if artifact_ref:
            parameters["artifact_ref"] = artifact_ref
            if deployment.runtime in {Runtime.DOCKER, Runtime.KUBERNETES}:
                parameters["image_repository"] = artifact_ref.split("@", 1)[0]
            elif deployment.runtime == Runtime.SYSTEMD and artifact_ref.startswith(("http://", "https://")):
                parameters["artifact_url"] = artifact_ref
        parameters["artifact_sha256"] = deployment.artifact_digest.removeprefix("sha256:")
        if deployment.fencing_token is not None:
            # The workflow carries this back on every callback. Without it a workflow that
            # timed out and resumed cannot be told apart from the one that replaced it.
            parameters["fencing_token"] = deployment.fencing_token
        # The workflow reports with a token minted for this deployment alone, so the
        # worker holds nothing that could report for any other. Minting is skipped only
        # when no signing key is configured, which local mode permits and nothing else does.
        if workload_identity.workload_identity_configured():
            parameters["callback_token"] = workload_identity.mint(
                workload=workload_identity.Workload.TEMPORAL,
                application_id=application.id,
                deployment_id=deployment.id,
                scopes={
                    workload_identity.Scope.DEPLOYMENT_RESULT,
                    workload_identity.Scope.DEPLOYMENT_READ,
                    workload_identity.Scope.CI_EVIDENCE,
                },
                # A production approval can wait a day; the token must outlive the wait.
                ttl_seconds=workload_identity.MAX_TTL_SECONDS,
            )
        return parameters

    def _start_cd(self, application: Application, run: PipelineRun, deployment: Deployment) -> Deployment:
        """Start the durable CD workflow for a deployment that is ready to move."""

        if deployment.status != DeploymentStatus.DEPLOYING:
            return deployment
        parameters = self._workflow_parameters(application, run, deployment)
        request = CdStartRequest(
            application_id=application.id,
            pipeline_run_id=run.id,
            deployment_id=deployment.id,
            runtime=deployment.runtime.value,
            environment=deployment.environment.value,
            artifact_digest=deployment.artifact_digest,
            release_name=application.name,
            require_approval=False,
            parameters=parameters,
            commit_sha=run.commit_sha,
            source_repository=application.repository_url,
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
        # The deploy stage is running from here; the result callback closes it.
        with self._transaction() as transaction:
            transaction.record_pipeline_stage(PipelineStage(
                pipeline_run_id=run.id, stage_id="deploy", stage_name="Deploy", status="running", started_at=_now(),
            ))
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

    def start_preview(self, preview_request: PreviewRequest) -> None:
        """Start (or restart) the durable preview workflow.

        Outside any transaction, like `_start_cd`: this reaches Temporal, and a database
        connection held open across that call is how a slow orchestrator becomes a
        connection-pool outage. A failure to start is not silently dropped -- the row is
        moved to `failed` and the reason audited, because a preview stuck in `deploying`
        with no worker coming is indistinguishable from one still on its way.
        """

        parameters = dict(preview_request.parameters)
        if preview_request.action == "deploy":
            with self._transaction() as transaction:
                evidence = transaction.security_evidence(preview_request.pipeline_run_id) or {}
            artifact_ref = str(evidence.get("artifactRef") or "").strip()
            if artifact_ref:
                parameters["artifact_ref"] = artifact_ref
                parameters["image_repository"] = artifact_ref.split("@", 1)[0]
        if workload_identity.workload_identity_configured():
            parameters["callback_token"] = workload_identity.mint(
                workload=workload_identity.Workload.TEMPORAL,
                application_id=preview_request.application_id,
                pipeline_run_id=preview_request.pipeline_run_id,
                scopes={workload_identity.Scope.PREVIEW_RESULT, workload_identity.Scope.CI_EVIDENCE},
                ttl_seconds=workload_identity.MAX_TTL_SECONDS,
            )
        request = PreviewStartRequest(
            preview_id=preview_request.preview_id,
            application_id=preview_request.application_id,
            pipeline_run_id=preview_request.pipeline_run_id,
            action=preview_request.action,
            namespace=preview_request.namespace,
            release=preview_request.release,
            artifact_digest=preview_request.artifact_digest,
            parameters=parameters,
        )
        try:
            self.cd_orchestrator.start_preview(request)
        except CdStartError as exc:
            with self._transaction() as transaction:
                transaction.update_preview_environment(
                    preview_request.preview_id,
                    status="failed",
                    detail=f"could not start the preview workflow: {exc}",
                )
                transaction.apply(UnitOfWork(audit=[AuditRecord(
                    "preview.start_failed",
                    application_id=preview_request.application_id,
                    pipeline_run_id=preview_request.pipeline_run_id,
                    payload={"previewId": preview_request.preview_id, "action": preview_request.action, "error": str(exc)},
                )]))
            return
        with self._transaction() as transaction:
            transaction.apply(UnitOfWork(audit=[AuditRecord(
                "preview.workflow_started",
                application_id=preview_request.application_id,
                pipeline_run_id=preview_request.pipeline_run_id,
                payload={"previewId": preview_request.preview_id, "action": preview_request.action},
            )]))

    def create_production_promotion(
        self,
        source_pipeline_run_id: UUID,
        *,
        requested_by: str,
        correlation_id: str,
        production_request_id: str,
        scheduled_for: datetime,
        rollback_strategy: str = "automatic",
        run_automation_tests: bool = True,
        deployment_parameters: dict[str, object] | None = None,
    ) -> Deployment:
        """Create an approval-bound production run from a verified release artifact."""

        request_payload = {
            "sourcePipelineRunId": str(source_pipeline_run_id),
            "requestedBy": requested_by,
            "correlationId": correlation_id,
            "scheduledFor": scheduled_for.isoformat(),
            "rollbackStrategy": rollback_strategy,
            "runAutomationTests": run_automation_tests,
            "deploymentParameters": dict(deployment_parameters or {}),
        }
        with self._transaction() as transaction:
            replay = self._idempotent_replay(
                transaction, "production.promotion", production_request_id, request_payload
            )
            if replay is not None:
                if not isinstance(replay, PipelineRun):
                    raise AssertionError("production promotion idempotency returned another result type")
                existing = transaction.deployments(pipeline_run_id=replay.id)
                if not existing:
                    raise DeliveryError(
                        "INCONSISTENT_PRODUCTION_PROMOTION",
                        "the production run exists without its deployment",
                        500,
                    )
                return existing[0]

            source = transaction.pipeline_run(source_pipeline_run_id)
            if source is None:
                raise DeliveryError("PIPELINE_NOT_FOUND", "pipeline run not found", 404)
            if source.status != PipelineStatus.SUCCEEDED or not source.artifact_digest:
                raise DeliveryError(
                    "VERSION_NOT_PROMOTABLE",
                    "source pipeline must have completed successfully with an immutable artifact",
                    409,
                )
            evidence = transaction.security_evidence(source.id)
            decision = evaluate_artifact_evidence(
                evidence,
                expected_digest=source.artifact_digest,
                require_evidence=True,
            )
            if not decision.allowed:
                raise DeliveryError("ARTIFACT_POLICY_DENIED", decision.reason, 422)
            application = transaction.application(source.application_id)
            if application is None:
                raise DeliveryError("APPLICATION_NOT_FOUND", "application not found", 404)
            return self._write_production_promotion(
                transaction,
                source=source,
                application=application,
                evidence=evidence,
                requested_by=requested_by,
                correlation_id=correlation_id,
                production_request_id=production_request_id,
                scheduled_for=scheduled_for,
                rollback_strategy=rollback_strategy,
                run_automation_tests=run_automation_tests,
                deployment_parameters=deployment_parameters,
                request_payload=request_payload,
            )

    def _write_production_promotion(
        self,
        transaction: PlatformSession,
        *,
        source: PipelineRun,
        application: Application,
        evidence: dict[str, object] | None,
        requested_by: str,
        correlation_id: str,
        production_request_id: str,
        scheduled_for: datetime,
        rollback_strategy: str,
        run_automation_tests: bool,
        deployment_parameters: dict[str, object] | None,
        request_payload: dict[str, object],
    ) -> Deployment:
        now = _now()
        run = PipelineRun(
            application_id=source.application_id,
            commit_sha=source.commit_sha,
            branch=source.branch,
            environment=Environment.PROD,
            parameters={
                **source.parameters,
                **dict(deployment_parameters or {}),
                "sourcePipelineRunId": str(source.id),
                "productionRequestId": production_request_id,
                "notBefore": scheduled_for.isoformat(),
                "rollbackStrategy": rollback_strategy,
                "runAutomationTests": run_automation_tests,
            },
            correlation_id=correlation_id,
            status=PipelineStatus.WAITING_APPROVAL,
            started_by=requested_by,
            artifact_digest=source.artifact_digest,
            created_at=now,
            updated_at=now,
        )
        deployment = Deployment(
            application_id=application.id,
            pipeline_run_id=run.id,
            runtime=application.runtime,
            environment=Environment.PROD,
            artifact_digest=source.artifact_digest,
            previous_artifact_digest=self._digest_in_service(transaction, application.id, Environment.PROD),
            status=DeploymentStatus.PENDING_APPROVAL,
            created_at=now,
            updated_at=now,
        )
        copied_evidence = {
            **dict(evidence or {}),
            "pipelineRunId": str(run.id),
            "sourcePipelineRunId": str(source.id),
        }
        unit = UnitOfWork(runs=[(run, None)], deployments=[(deployment, None)])
        unit.idempotency.append(
            IdempotencyRow(
                scope="production.promotion",
                idempotency_key=production_request_id,
                request_hash=self._payload_hash(request_payload),
                resource_type="pipeline_run",
                resource_id=run.id,
                response_status=202,
            )
        )
        unit.security_evidence.append((run.id, application.id, source.artifact_digest, copied_evidence))
        unit.logs.append((run.id, [f"production promotion sourcePipelineRunId={source.id}"]))
        unit.audit.append(
            AuditRecord(
                "production.promotion_created",
                application_id=application.id,
                pipeline_run_id=run.id,
                deployment_id=deployment.id,
                actor=requested_by,
                correlation_id=correlation_id,
                payload={
                    "productionRequestId": production_request_id,
                    "sourcePipelineRunId": str(source.id),
                    "artifactDigest": source.artifact_digest,
                    "scheduledFor": scheduled_for.isoformat(),
                    "rollbackStrategy": rollback_strategy,
                    "runAutomationTests": run_automation_tests,
                },
            )
        )
        self._apply(transaction, unit)
        return deployment

    # -------------------------------------------------------------- approvals

    def deployment_requested_by(self, deployment_id: UUID) -> str:
        """The subject that started the run this deployment came from, or "".

        Returns "" when the run predates authentication or the deployment was not created
        from a run. `require_separation_of_duties` reads that as "cannot be shown to be
        the same person" and permits the approval, rather than blocking every deployment
        that existed before the upgrade.
        """

        with self._transaction() as transaction:
            deployment = transaction.deployment(deployment_id)
            if deployment is None or deployment.pipeline_run_id is None:
                return ""
            run = transaction.pipeline_run(deployment.pipeline_run_id)
        return (run.started_by or "") if run else ""

    def approve_deployment(self, deployment_id: UUID, actor: str) -> Deployment:
        with self._transaction() as transaction:
            updated, resumed_run, application = self._write_approval(transaction, deployment_id, actor)
        # Signalling or starting the workflow reaches Temporal, so it happens after the
        # approval is durable. A crash here leaves an approved deployment whose workflow
        # has not started -- recoverable -- rather than a started workflow nobody approved.
        if resumed_run is not None and application is not None:
            self._resume_cd_after_approval(resumed_run, updated, application, actor)
        return updated

    def _write_approval(
        self, transaction: PlatformSession, deployment_id: UUID, actor: str
    ) -> tuple[Deployment, PipelineRun | None, Application | None]:
        deployment = transaction.deployment(deployment_id)
        if deployment is None:
            raise DeliveryError("DEPLOYMENT_NOT_FOUND", "deployment not found", 404)
        if deployment.status != DeploymentStatus.PENDING_APPROVAL:
            raise DeliveryError("INVALID_DEPLOYMENT_STATE", "deployment is not waiting for approval", 409)

        now = _now()
        run_for_target = (
            transaction.pipeline_run(deployment.pipeline_run_id)
            if deployment.pipeline_run_id
            else None
        )
        # An approval is what starts a production deployment moving, so the freeze is
        # checked here: approving the day before a freeze does not deploy during it.
        self._refuse_during_freeze(transaction, deployment.application_id, deployment.environment)
        unit = UnitOfWork()
        # An approval is what starts a production deployment moving, so the target is
        # claimed here. Two approvals seconds apart used to produce two workflows writing
        # to the same hosts; now the second one is refused.
        lease = self._acquire_lease(
            transaction, unit, deployment, run_for_target, owner=f"approval:{actor}"
        )
        updated = replace(
            deployment,
            status=DeploymentStatus.DEPLOYING,
            approved_by=actor,
            fencing_token=lease.fencing_token,
            version=deployment.version + 1,
            updated_at=now,
        )
        unit.deployments = [(updated, deployment.version)]
        unit.audit.append(
            AuditRecord(
                "deployment.approved",
                application_id=deployment.application_id,
                pipeline_run_id=deployment.pipeline_run_id,
                deployment_id=deployment.id,
                actor=actor,
            )
        )
        resumed: PipelineRun | None = None
        application: Application | None = None
        if deployment.pipeline_run_id is not None:
            run = transaction.pipeline_run(deployment.pipeline_run_id)
            if run is None:
                raise DeliveryError("PIPELINE_NOT_FOUND", "pipeline run not found", 404)
            resumed = replace(run, status=PipelineStatus.RUNNING, version=run.version + 1, updated_at=now)
            unit.runs.append((resumed, run.version))
            unit.logs.append((run.id, [f"approved by={actor}"]))
            application = transaction.application(deployment.application_id)
        self._apply(transaction, unit)
        return updated, resumed, application

    def _resume_cd_after_approval(
        self, run: PipelineRun, deployment: Deployment, application: Application, actor: str
    ) -> None:
        """Signal the waiting workflow, or start one if approval came before it existed."""

        if run.workflow_id:
            try:
                self.cd_orchestrator.signal_approval(run.workflow_id, actor, "approved via netCI")
            except CdStartError as exc:
                raise DeliveryError("CD_SIGNAL_FAILED", str(exc), 502) from exc
            return
        self._start_cd(application, run, deployment)

    # ------------------------------------------------------ deployment results

    def record_deployment_result(
        self,
        deployment_id: UUID,
        result_status: str,
        message: str | None,
        fencing_token: int | None = None,
    ) -> Deployment:
        with self._transaction() as transaction:
            return self._write_deployment_result(
                transaction, deployment_id, result_status, message, fencing_token
            )

    def _write_deployment_result(
        self,
        transaction: PlatformSession,
        deployment_id: UUID,
        result_status: str,
        message: str | None,
        fencing_token: int | None = None,
    ) -> Deployment:
        deployment = transaction.deployment(deployment_id)
        if deployment is None:
            raise DeliveryError("DEPLOYMENT_NOT_FOUND", "deployment not found", 404)
        # Before anything else: is this writer still the one that owns the deployment?
        # A workflow that timed out and came back must not overwrite its replacement.
        self._reject_stale_writer(transaction, deployment, fencing_token, "deployment.result")
        if deployment.status.value == result_status and deployment.status in {
            DeploymentStatus.HEALTHY,
            DeploymentStatus.FAILED,
        }:
            return deployment
        if deployment.status != DeploymentStatus.DEPLOYING:
            raise DeliveryError("INVALID_DEPLOYMENT_STATE", "deployment is not deploying", 409)
        if result_status not in {DeploymentStatus.HEALTHY.value, DeploymentStatus.FAILED.value}:
            raise DeliveryError("INVALID_DEPLOYMENT_RESULT", "deployment result is not supported", 422)

        now = _now()
        target_status = DeploymentStatus(result_status)
        healthy = target_status == DeploymentStatus.HEALTHY
        updated = replace(
            deployment, status=target_status, version=deployment.version + 1, updated_at=now,
            healthy_at=(deployment.healthy_at or now) if healthy else deployment.healthy_at,
        )
        unit = UnitOfWork(deployments=[(updated, deployment.version)])
        run = (
            transaction.pipeline_run(deployment.pipeline_run_id)
            if deployment.pipeline_run_id
            else None
        )
        # A deployment decides its run's outcome only when it is the run's own CD -- the
        # run is still running or waiting for approval. A promotion or redeploy of an
        # artifact that was built long ago reuses that run to locate the artifact; it used
        # to overwrite a succeeded build as `failed` when the later deployment failed,
        # which made the artifact undeployable and the environment look empty.
        owns_run = run is not None and run.status in {PipelineStatus.RUNNING, PipelineStatus.WAITING_APPROVAL}
        if run is not None:
            # Audited for every deployment, whoever's run it borrowed.
            unit.audit.append(
                AuditRecord(
                    f"deployment.{target_status.value}",
                    application_id=deployment.application_id,
                    pipeline_run_id=run.id,
                    deployment_id=deployment.id,
                    correlation_id=run.correlation_id,
                )
            )
        if run is not None and not owns_run:
            unit.logs.append((run.id, [
                f"deployment {deployment.id} of this artifact to {deployment.environment.value}: {target_status.value}"
                + (f" ({message})" if message else "")
            ]))
        if run is not None and owns_run:
            pipeline_status = PipelineStatus.SUCCEEDED if healthy else PipelineStatus.FAILED
            unit.runs.append(
                (replace(run, status=pipeline_status, version=run.version + 1, updated_at=now), run.version)
            )
            if message:
                unit.logs.append((run.id, [f"deployment={target_status.value} {message}"]))
            # The run's last two stages are netCI's, not Jenkins': the deployment and
            # its health check. Recorded here so the Portal's stage graph ends where
            # the release did instead of stopping at "publish".
            stage_status = "succeeded" if healthy else "failed"
            for stage_id, stage_name in (("deploy", "Deploy"), ("health-check", "Health Check")):
                transaction.record_pipeline_stage(PipelineStage(
                    pipeline_run_id=run.id, stage_id=stage_id, stage_name=stage_name, status=stage_status,
                    completed_at=now, error_message=None if healthy else (message or None),
                ))
        self._record_delivery_outcome(
            transaction, unit, updated, run, healthy=healthy, occurred_at=now
        )
        unit.notifications.append(
            NotificationRecord(
                id=uuid4(),
                event_type="deployment.completed",
                aggregate_type="deployment",
                aggregate_id=str(deployment.id),
                payload={
                    "application_id": str(deployment.application_id),
                    "environment": deployment.environment.value,
                    "status": target_status.value,
                    "artifact_digest": deployment.artifact_digest,
                },
                recipient="events@netci.local",
            )
        )
        if run is not None and owns_run:
            # The pipeline completed only if this deployment was its own CD.
            unit.notifications.append(
                NotificationRecord(
                    id=uuid4(),
                    event_type="pipeline.completed",
                    aggregate_type="pipeline_run",
                    aggregate_id=str(run.id),
                    payload={
                        "application_id": str(run.application_id),
                        "status": pipeline_status.value,
                        "artifact_digest": run.artifact_digest,
                    },
                    recipient="events@netci.local",
                )
            )
        # Terminal either way: hand the target back rather than making the next
        # deployment wait out the lease expiry.
        self._release_lease(transaction, unit, updated, reason=target_status.value)
        self._apply(transaction, unit)
        return updated

    def rollback_deployment(self, deployment_id: UUID, target_artifact_digest: str) -> Deployment:
        executes = getattr(self.cd_orchestrator, "mode", "none") != "none"
        with self._transaction() as transaction:
            source = None
            if executes:
                # The worker locates and re-verifies the target through the evidence of
                # the run that built it. No such run means netCI has no reference for
                # that digest, and a rollback to an artifact it cannot find is refused
                # rather than recorded as in progress and left there.
                current = transaction.deployment(deployment_id)
                if current is None:
                    raise DeliveryError("DEPLOYMENT_NOT_FOUND", "deployment not found", 404)
                source = self._run_that_built(transaction, current.application_id, target_artifact_digest)
                if source is None:
                    raise DeliveryError(
                        "ROLLBACK_ARTIFACT_UNKNOWN",
                        f"no succeeded run of this application built {target_artifact_digest}; "
                        "netCI cannot locate or verify it",
                        409,
                    )
            deployment = self._write_rollback(transaction, deployment_id, target_artifact_digest)
        if not executes or source is None:
            return deployment
        # Outside the transaction: this reaches Temporal.
        return self._start_rollback_cd(deployment, source)

    def source_run_in_service(self, application_id: UUID, environment: Environment) -> PipelineRun | None:
        """The run that built what this environment is serving right now.

        After a rollback the newest deployment record carries the *restored* digest and
        its run is marked rolled_back; "the latest deployment's run" would then name the
        release that was pulled. Start from the digest in service and find its builder.
        """

        with self._transaction() as transaction:
            digest = self._digest_in_service(transaction, application_id, environment)
            if digest is None:
                return None
            return self._run_that_built(transaction, application_id, digest)

    @staticmethod
    def _run_that_built(
        transaction: PlatformSession, application_id: UUID, artifact_digest: str
    ) -> PipelineRun | None:
        candidates = [
            run
            for run in transaction.pipeline_runs(application_id)
            if run.artifact_digest == artifact_digest
            and run.status in {PipelineStatus.SUCCEEDED, PipelineStatus.ROLLED_BACK}
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda run: run.created_at)

    def _start_rollback_cd(self, deployment: Deployment, source: PipelineRun) -> Deployment:
        """Start RollbackWorkflow, or record that the rollback could not start.

        `rollback_in_progress` with nothing running is the state this replaces; if Temporal
        refuses the workflow the deployment is moved to `rollback_failed` with the reason,
        which is the truth and is what pages someone.
        """

        application = self.get_application(deployment.application_id)
        parameters = self._workflow_parameters(application, source, deployment)
        request = CdStartRequest(
            application_id=application.id,
            pipeline_run_id=source.id,
            deployment_id=deployment.id,
            runtime=deployment.runtime.value,
            environment=deployment.environment.value,
            artifact_digest=deployment.artifact_digest,
            release_name=application.name,
            require_approval=False,
            parameters=parameters,
            commit_sha=source.commit_sha,
            source_repository=application.repository_url,
        )
        try:
            workflow_id = self.cd_orchestrator.start_rollback(request)
        except CdStartError as exc:
            self.record_rollback_result(
                deployment.id, False, f"rollback workflow could not start: {exc}",
                fencing_token=deployment.fencing_token,
            )
            raise DeliveryError("CD_START_FAILED", str(exc), 502) from exc
        if workflow_id is None:
            return deployment
        unit = UnitOfWork()
        if deployment.pipeline_run_id is not None:
            unit.logs.append((deployment.pipeline_run_id, [f"rollback-workflow-started workflowId={workflow_id} sourceRun={source.id}"]))
        unit.audit.append(
            AuditRecord(
                "deployment.rollback_workflow_started",
                application_id=application.id,
                pipeline_run_id=deployment.pipeline_run_id,
                deployment_id=deployment.id,
                payload={"workflowId": workflow_id, "sourcePipelineRunId": str(source.id)},
            )
        )
        self._commit(unit)
        return deployment

    def _write_rollback(
        self, transaction: PlatformSession, deployment_id: UUID, target_artifact_digest: str
    ) -> Deployment:
        deployment = transaction.deployment(deployment_id)
        if deployment is None:
            raise DeliveryError("DEPLOYMENT_NOT_FOUND", "deployment not found", 404)
        if deployment.status not in {
            DeploymentStatus.HEALTHY,
            DeploymentStatus.FAILED,
            DeploymentStatus.ROLLBACK_FAILED,
        }:
            raise DeliveryError(
                "INVALID_DEPLOYMENT_STATE",
                "only a healthy, failed or rollback-failed deployment can be rolled back",
                409,
            )
        if not IMMUTABLE_DIGEST.fullmatch(target_artifact_digest):
            raise DeliveryError("IMMUTABLE_ARTIFACT_REQUIRED", "rollback requires a sha256 artifact digest", 422)
        run = (
            transaction.pipeline_run(deployment.pipeline_run_id)
            if deployment.pipeline_run_id
            else None
        )

        now = _now()
        was_healthy = deployment.status == DeploymentStatus.HEALTHY
        # A rollback writes to the same target the deployment did, so it needs the same
        # exclusion. The lease is normally free by now -- the result released it -- but
        # a rollback racing a redeploy must still be refused.
        lease_unit = UnitOfWork()
        lease = self._acquire_lease(
            transaction, lease_unit, deployment, run, owner="rollback"
        )
        # `rollback_in_progress`, not `rolled_back`: this call *starts* the rollback. The
        # old version is not back until the workflow says so, and a platform that records
        # the restore before it happened is exactly the false-green this project exists to
        # remove. `record_rollback_result` writes the outcome and the DORA event.
        updated = replace(
            deployment,
            status=DeploymentStatus.ROLLBACK_IN_PROGRESS,
            fencing_token=lease.fencing_token,
            previous_artifact_digest=deployment.artifact_digest,
            artifact_digest=target_artifact_digest,
            version=deployment.version + 1,
            updated_at=now,
        )
        unit = UnitOfWork(deployments=[(updated, deployment.version)])
        unit.audit.extend(lease_unit.audit)
        unit.audit.append(
            AuditRecord(
                "deployment.rollback_started",
                application_id=deployment.application_id,
                pipeline_run_id=deployment.pipeline_run_id,
                deployment_id=deployment.id,
                payload={"targetArtifactDigest": target_artifact_digest},
            )
        )
        if run is not None:
            unit.logs.append((run.id, [f"rolling back to {target_artifact_digest}"]))

        if deployment.environment == Environment.PROD and was_healthy:
            # A healthy release that had to be pulled is a change failure, and that is
            # true the moment the decision is made. Whether service is restored is a
            # separate fact, recorded by `record_rollback_result`.
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
        self._apply(transaction, unit)
        return updated

    def record_rollback_result(
        self,
        deployment_id: UUID,
        succeeded: bool,
        message: str | None = None,
        fencing_token: int | None = None,
    ) -> Deployment:
        """Report whether a rollback actually restored service.

        A rollback that failed leaves the environment in neither the new state nor the old
        one. Recording that as `failed` would lose the fact that recovery was attempted
        and did not work -- which is exactly what the person deciding whether to page
        someone needs to know. It also must not emit a DORA recovery event: nothing was
        restored.
        """

        with self._transaction() as transaction:
            deployment = transaction.deployment(deployment_id)
            if deployment is None:
                raise DeliveryError("DEPLOYMENT_NOT_FOUND", "deployment not found", 404)
            self._reject_stale_writer(transaction, deployment, fencing_token, "rollback.result")
            if deployment.status not in {
                DeploymentStatus.ROLLBACK_IN_PROGRESS,
                DeploymentStatus.ROLLED_BACK,
                DeploymentStatus.ROLLBACK_FAILED,
            }:
                raise DeliveryError(
                    "INVALID_DEPLOYMENT_STATE", "this deployment is not rolling back", 409
                )
            target = (
                DeploymentStatus.ROLLED_BACK if succeeded else DeploymentStatus.ROLLBACK_FAILED
            )
            if deployment.status == target:
                return deployment
            now = _now()
            updated = replace(
                deployment, status=target, version=deployment.version + 1, updated_at=now
            )
            unit = UnitOfWork(deployments=[(updated, deployment.version)])
            unit.audit.append(
                AuditRecord(
                    f"deployment.{target.value}",
                    application_id=deployment.application_id,
                    pipeline_run_id=deployment.pipeline_run_id,
                    deployment_id=deployment.id,
                    payload={"message": message} if message else {},
                )
            )
            run = (
                transaction.pipeline_run(deployment.pipeline_run_id)
                if deployment.pipeline_run_id
                else None
            )
            if run is not None:
                # The run follows its deployment: a restored release is not a success.
                unit.runs.append(
                    (
                        replace(
                            run,
                            status=PipelineStatus.ROLLED_BACK
                            if succeeded
                            else PipelineStatus.FAILED,
                            version=run.version + 1,
                            updated_at=now,
                        ),
                        run.version,
                    )
                )
                if message:
                    unit.logs.append((run.id, [f"rollback={target.value} {message}"]))
            if succeeded and deployment.environment == Environment.PROD:
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
            self._release_lease(transaction, unit, updated, reason=target.value)
            self._apply(transaction, unit)
            return updated

    def _record_delivery_outcome(
        self,
        transaction: PlatformSession,
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
        for failure in self._unrecovered_failures(
            transaction, deployment.application_id, deployment.environment
        ):
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

    def _unrecovered_failures(
        self, transaction: PlatformSession, application_id: UUID, environment: Environment
    ) -> list[DeliveryEvent]:
        history = transaction.delivery_events(application_id)
        recovered = {
            event.deployment_id
            for event in history
            if event.event_type == DeliveryEventType.RECOVERY
            and event.application_id == application_id
            and event.environment == environment
        }
        return [
            event
            for event in history
            if event.event_type == DeliveryEventType.DEPLOYMENT
            and event.application_id == application_id
            and event.environment == environment
            and event.requires_intervention
            and event.deployment_id not in recovered
        ]
