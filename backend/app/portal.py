"""Release Portal queries and commands.

This layer adapts the delivery domain (applications, pipeline runs and deployments) to the
System -> Module -> Release hierarchy the Custom Portal shows. It is isolated from
transport, and it reads and writes the same store the delivery domain does.

It is a service, not a read model: it creates systems, attaches modules, registers
versions and approves production requests. It was previously named `PortalReadModel`,
which hid exactly the thing a reader needs to know about a class that mutates state.
Every command reads the rows it is about to change inside the transaction that writes
them, so no dictionary in this process decides anything durable.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

from .adapters.dcim import DcimCatalog, build_dcim_catalog
from .demo_data import seed_demo_data
from .runtime_environment import is_local_runtime
from .errors import ApiError
from .delivery import DeliveryPlatform
from .persistence import (
    AuditRecord,
    ConcurrentModification,
    StillReferenced,
    UnitOfWork,
    VersionConflict,
)
from .projections.dora import DoraEvent, project_dora
from .domain.models import (
    ConfigRevisionStatus,
    DeliveryEvent,
    DeploymentStatus,
    Environment,
    ModuleConfigRevision,
    NotificationRecord,
    PipelineStatus,
    Runtime,
)
from .store import (
    ModuleRow,
    PlatformDatabase,
    PlatformSession,
    RequestModuleRow,
    RequestRow,
    SystemRow,
    VersionRow,
    join,
)
from .domain.dag import compute_dag_waves, DagValidationError
from .coordinator import ReleasePlanCoordinator

#: Rolling window every DORA figure is computed over. Stated in the response so a
#: number on screen can never be read without the period it belongs to.
DORA_WINDOW_DAYS = 30


DB_URI_REGEX = re.compile(
    r"(postgres(?:ql)?|mysql|mongodb|redis|oracle|jdbc|sqlserver)://[^\s\"']+",
    re.IGNORECASE,
)
PRIVATE_KEY_REGEX = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----", re.IGNORECASE)
SQL_DDL_REGEX = re.compile(
    r"\b(alter\s+table|drop\s+table|create\s+table|truncate\s+table)\b",
    re.IGNORECASE,
)

CRITICAL_KEYWORDS = (
    "schema",
    "encryption",
    "private_key",
    "db_password",
    "database_password",
    "db_connection",
    "datasource_url",
    "root_secret",
    "master_key",
    "db_url",
    "connection_string",
    "secret_key",
    "root_password",
    "schema_migration",
)


def _scan_obj_for_critical_risk(obj: Any, depth: int = 0) -> list[str]:
    """Recursively scan nested structures, keys, values, and base64-encoded strings for critical security content."""
    reasons: list[str] = []
    if depth > 20:
        return reasons

    if isinstance(obj, dict):
        for k, v in obj.items():
            k_norm = str(k).lower().replace("-", "_").strip()
            for kw in CRITICAL_KEYWORDS:
                if kw in k_norm:
                    reasons.append(f"Contains critical security key '{k}'")
            reasons.extend(_scan_obj_for_critical_risk(v, depth + 1))
    elif isinstance(obj, (list, tuple, set)):
        for item in obj:
            reasons.extend(_scan_obj_for_critical_risk(item, depth + 1))
    elif isinstance(obj, str):
        val = obj.strip()
        if not val:
            return reasons
        val_lower = val.lower()

        if DB_URI_REGEX.search(val):
            reasons.append("Contains database connection string / datasource URI")
        if PRIVATE_KEY_REGEX.search(val):
            reasons.append("Contains private cryptographic key material")
        if SQL_DDL_REGEX.search(val):
            reasons.append("Contains raw database schema DDL instructions")
        for kw in ("private_key", "db_password", "database_password", "root_secret", "master_key"):
            if kw in val_lower:
                reasons.append(f"Contains critical keyword '{kw}' in value")

        # Base64 detection and decode inspection
        if len(val) >= 12 and len(val) % 4 == 0 and re.match(r"^[A-Za-z0-9+/]+={0,2}$", val):
            try:
                decoded = base64.b64decode(val, validate=True).decode("utf-8", errors="ignore").lower()
                if DB_URI_REGEX.search(decoded):
                    reasons.append("Contains base64-encoded database connection URI")
                if "private key" in decoded:
                    reasons.append("Contains base64-encoded private key")
                for kw in ("db_password", "password", "secret", "datasource", "schema"):
                    if kw in decoded:
                        reasons.append(f"Contains base64-encoded sensitive term '{kw}'")
            except Exception:
                pass

    return reasons


def _without_nulls(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _without_nulls(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [_without_nulls(v) for v in value]
    return value.value if isinstance(value, Environment) else value


# Every delivery parameter the server derives from the module's reviewed configuration.
# A retry strips these from the parent run before re-binding, so a key a superseded
# revision carried (an old app_root, a removed host_port) cannot ride along.
SERVER_OWNED_DELIVERY_KEYS = frozenset({
    "app_root", "host_port", "container_port", "network_mode", "app_port", "health_url",
    "systemd_scope", "netci_become", "image_pull_host",
    "app_name", "target_environment", "target_hosts", "deployment_tasks", "task_settings",
    "runtime_health_verified", "target_namespace", "kubeconfig_ref",
})


def runtime_parameters(target: dict[str, Any]) -> dict[str, object]:
    """Playbook inputs from a target's reviewed `runtimeSettings`.

    Field by field, by name: nothing else in the target reaches the playbook, so a key
    a revision carries for the risk classifier's benefit can never become a variable.
    The names are the playbooks' own (`app_root`, `host_port`, ...); a caller cannot
    supply them per run -- build_inputs rejects every one of them.
    """

    settings = target.get("runtimeSettings")
    if not isinstance(settings, dict):
        return {}
    out: dict[str, object] = {}
    if settings.get("appRoot"):
        out["app_root"] = str(settings["appRoot"])
    if settings.get("hostPort") is not None:
        out["host_port"] = int(settings["hostPort"])
    if settings.get("containerPort") is not None:
        out["container_port"] = int(settings["containerPort"])
    if settings.get("networkMode"):
        out["network_mode"] = str(settings["networkMode"])
    if settings.get("appPort") is not None:
        port = int(settings["appPort"])
        out["app_port"] = port
        # The systemd playbook's health gate probes this URL; deriving it here keeps the
        # port and the probe in agreement by construction.
        out["health_url"] = f"http://127.0.0.1:{port}/healthz"
    if settings.get("systemdScope"):
        out["systemd_scope"] = str(settings["systemdScope"])
    if settings.get("become") is not None:
        out["netci_become"] = bool(settings["become"])
    if settings.get("imagePullHost"):
        out["image_pull_host"] = str(settings["imagePullHost"])
    return out


def classify_config_risk(
    pipeline_config: dict[str, Any],
    deployment_config: list[dict[str, Any]],
    target_env: str = "staging",
) -> tuple[str, list[str]]:
    """Classify configuration risk class:
    - critical: schema change, credentials/passwords, private keys, database datasource changes
    - high: auth policy, removing security gates, production target servers
    - medium: timeout, rate limit, worker concurrency, resource limits
    - low: log level, feature flag, replica count
    """
    reasons: list[str] = []

    # 1. Recursive critical risk scanning (nested dicts, encoded secrets, DB URIs, private keys)
    reasons.extend(_scan_obj_for_critical_risk(pipeline_config))
    reasons.extend(_scan_obj_for_critical_risk(deployment_config))

    # Also string dump check for legacy/generic matches
    p_str = json.dumps(pipeline_config, default=str).lower()
    d_str = json.dumps(deployment_config, default=str).lower()
    for kw in CRITICAL_KEYWORDS:
        if kw in p_str or kw in d_str:
            reasons.append(f"Contains critical security keyword '{kw}'")

    if reasons:
        return "critical", list(dict.fromkeys(reasons))

    # 2. High risk
    for kw in ("auth_policy", "oauth", "jwt_secret", "allow_anonymous", "bypass_auth"):
        if kw in p_str or kw in d_str:
            reasons.append(f"Touches sensitive authentication/authorization policy '{kw}'")

    stages = pipeline_config.get("stages", [])
    if isinstance(stages, list) and stages:
        if "vulnerability-scan" not in stages:
            reasons.append("Pipeline configuration omits 'vulnerability-scan' security gate")
        if "sbom" not in stages:
            reasons.append("Pipeline configuration omits 'sbom' compliance gate")

    if target_env.lower() == "prod":
        reasons.append("Targeting production environment")

    if reasons:
        return "high", list(dict.fromkeys(reasons))

    # 3. Medium risk
    for kw in ("timeout", "rate_limit", "cpu_limit", "mem_limit", "concurrency", "replicas"):
        if kw in p_str or kw in d_str:
            reasons.append(f"Modifies operational boundary '{kw}'")

    if reasons:
        return "medium", list(dict.fromkeys(reasons))

    return "low", ["Safe configuration adjustment (feature flags / log level)"]


class PortalError(ApiError):
    """A Portal refusal: the client gets this code and status."""


class PortalService:
    """Portal queries and commands, answered from the store on every call."""

    def __init__(
        self,
        platform: DeliveryPlatform,
        *,
        database: PlatformDatabase | None = None,
        dcim_catalog: DcimCatalog | None = None,
    ) -> None:
        self.platform = platform
        # Sharing the platform's database is what lets onboarding write the delivery
        # application and the Portal module in one transaction.
        self.database: PlatformDatabase = database if database is not None else platform.database
        self.dcim_catalog = dcim_catalog or build_dcim_catalog()

    def reset(self) -> None:
        """Clear the in-memory store and re-apply demo data; local/test setup only."""

        clear = getattr(self.database, "clear", None)
        if clear is None:
            raise RuntimeError("reset() is only available for the in-memory store")
        clear()
        seed_demo_data(self.platform, self)

    def persistence_health(self) -> dict[str, str]:
        mode = self.database.describe()
        status = self.database.health()
        if status != "ok":
            return {"mode": mode, "status": "degraded", "message": status}
        return {"mode": mode, "status": "ready"}

    @contextmanager
    def _session(self, session: PlatformSession | None = None):
        """Open one transaction and translate its storage failures into API answers."""

        try:
            with join(self.database, session) as transaction:
                yield transaction
        except (ApiError, KeyError, ValueError):
            # A decided answer, including one from a delivery command composed into this
            # transaction. Only a genuine storage failure becomes a 503 below.
            raise
        except StillReferenced as exc:
            # Not a storage failure: the write was refused because something still points
            # at the row. 503 would send an operator to check the database when the answer
            # is "remove the production request first".
            raise PortalError("STILL_REFERENCED", str(exc), 409) from exc
        except ConcurrentModification as exc:
            raise PortalError("CONCURRENT_MODIFICATION", str(exc), 409) from exc
        except Exception as exc:
            raise PortalError(
                "PERSISTENCE_UNAVAILABLE", f"cannot reach portal state: {exc}", 503
            ) from exc

    # ---------------------------------------------------------------- hierarchy

    def create_system(self, *, system_id: str, unit: str, description: str, owner: str) -> dict[str, object]:
        with self._session() as transaction:
            if transaction.portal_system(system_id) is not None:
                raise ValueError("system already exists")
            transaction.insert_portal_system(
                SystemRow(system_id, unit, description, owner, "unknown")
            )
            return self._system(transaction, system_id)

    def remove_system(self, system_id: str) -> None:
        """Detach a system and its modules from the Portal.

        The referential check and the delete happen in one transaction, so a production
        request created between them cannot end up naming a module nobody can look up.
        """

        with self._session() as transaction:
            if transaction.portal_system(system_id) is None:
                raise KeyError("system not found")
            referenced = transaction.portal_modules_referenced_by_requests()
            if any(module.id in referenced for module in transaction.portal_modules(system_id)):
                raise PortalError(
                    "STILL_REFERENCED",
                    f"system {system_id} has modules referenced by production requests",
                    409,
                )
            transaction.delete_portal_system(system_id)

    def remove_module(self, module_id: str) -> None:
        """Detach one module from the Portal.

        Delivery history is deliberately preserved: the underlying application, its runs
        and its deployments stay, because an audit trail that disappears when someone
        tidies up the Portal is not an audit trail.
        """

        with self._session() as transaction:
            if transaction.portal_module(module_id) is None:
                raise KeyError("module not found")
            if module_id in transaction.portal_modules_referenced_by_requests():
                raise PortalError(
                    "STILL_REFERENCED",
                    f"module {module_id} is still referenced by a production request",
                    409,
                )
            transaction.delete_portal_module(module_id)

    def update_module(
        self,
        module_id: str,
        *,
        name: str,
        module_type: str,
        description: str,
    ) -> dict[str, object]:
        with self._session() as transaction:
            if transaction.portal_module(module_id) is None:
                raise KeyError("module not found")
            transaction.update_portal_module(
                module_id, name=name, module_type=module_type, description=description
            )
            return self._module(transaction, module_id)

    def attach_module(
        self,
        *,
        system_id: str,
        module_id: str,
        name: str,
        module_type: str,
        description: str,
        runtime: Runtime,
        application_id: UUID,
        deployment_environments: list[dict[str, object]],
        pipeline_config: dict[str, object],
        created_by: str | None = None,
        session: PlatformSession | None = None,
    ) -> dict[str, object]:
        with self._session(session) as transaction:
            self._validate_module_slot(transaction, system_id, module_id)
            transaction.insert_portal_module(
                ModuleRow(
                    id=module_id,
                    system_id=system_id,
                    name=name,
                    module_type=module_type,
                    description=description,
                    runtime=runtime.value,
                    application_id=application_id,
                    deployment_config=list(deployment_environments),
                    pipeline_config=dict(pipeline_config),
                )
            )
            # Revision 1 is written in the same transaction as the module. A module whose
            # configuration has no revision cannot be pinned to a run, and a run that
            # cannot name the configuration it used cannot be reproduced or audited.
            revision = transaction.record_config_revision(
                ModuleConfigRevision(
                    module_id=module_id,
                    revision_number=1,
                    created_by=created_by or "system",
                    pipeline_config=dict(pipeline_config),
                    deployment_config=list(deployment_environments),
                    change_summary="initial configuration recorded at onboarding",
                    status=ConfigRevisionStatus.ACTIVE,
                )
            )
            transaction.set_module_active_revision(module_id, revision.id, 1)
            return self._module(transaction, module_id)

    def validate_module_slot(self, system_id: str, module_id: str) -> None:
        with self._session() as transaction:
            self._validate_module_slot(transaction, system_id, module_id)

    @staticmethod
    def _validate_module_slot(transaction: PlatformSession, system_id: str, module_id: str) -> None:
        if transaction.portal_system(system_id) is None:
            raise KeyError("system not found")
        if transaction.portal_module(module_id) is not None:
            raise ValueError("module already exists")

    # ------------------------------------------------------ deploy-time revalidation

    @staticmethod
    def dcim_revalidation_required() -> bool:
        """Whether a target DCIM cannot vouch for blocks the deployment.

        Default on outside local mode. The point of asking DCIM at all is that the
        inventory changes between the moment a release is approved and the moment it
        deploys -- a host gets decommissioned, moved to another system, or put into
        maintenance. Asking and then ignoring the answer is worse than not asking.
        """

        setting = os.getenv("NETCI_REQUIRE_DCIM_REVALIDATION", "").strip().lower()
        if setting:
            return setting not in {"0", "false", "no"}
        return not is_local_runtime()

    def revalidate_deployment_targets(
        self, module_id: str, environment: Environment
    ) -> list[dict[str, object]]:
        """Ask DCIM whether these targets are still deployable, right before deploying.

        Raises `DEPLOYMENT_TARGET_INVALID` when a target has been decommissioned, moved
        to a different system or module, or put into maintenance. An unconfigured DCIM
        reports `unconfigured` and is not treated as approval -- it is reported as such,
        and blocks only when revalidation is required.
        """

        with self._session() as transaction:
            module = transaction.portal_module(module_id)
            if module is None:
                raise KeyError("module not found")
            target = next(
                (
                    item
                    for item in module.deployment_config
                    if (
                        item.get("environment").value
                        if isinstance(item.get("environment"), Environment)
                        else str(item.get("environment"))
                    ) == environment.value
                ),
                None,
            )
            system_id = module.system_id

        if target is None:
            raise PortalError(
                "DEPLOYMENT_TARGET_NOT_CONFIGURED",
                f"module {module_id} has no {environment.value} deployment target",
                409,
            )

        hosts = [str(item) for item in (target.get("servers") or [])]
        namespace = str(target.get("namespace") or "").strip()
        checked: list[dict[str, object]] = []
        blocked: list[str] = []
        for name in hosts or ([namespace] if namespace else []):
            result = self.dcim_catalog.validate_target(
                system_id, module_id, environment.value, name
            )
            checked.append(
                {
                    "target": name,
                    "valid": result.valid,
                    "status": result.status,
                    "message": result.message,
                }
            )
            # `unconfigured` is not a failure of the target -- it is a failure to have an
            # inventory. It blocks only where revalidation is required, and says which.
            if result.status == "unconfigured":
                if self.dcim_revalidation_required():
                    blocked.append(f"{name}: DCIM is not configured, so this target cannot be verified")
                continue
            if not result.valid:
                blocked.append(f"{name}: {result.message}")

        if blocked and self.dcim_revalidation_required():
            raise PortalError(
                "DEPLOYMENT_TARGET_INVALID",
                "the registered deployment targets are no longer deployable: "
                + "; ".join(blocked),
                409,
            )
        return checked

    def collect_server_health(self, server_names: list[str]) -> list[dict[str, object]]:
        """Record what the health provider says, and store `unknown` when there is none.

        A target with no provider is `unknown`, never `online`. "We have not looked" and
        "we looked and it is fine" are different facts, and only one of them justifies
        deploying on top of it.
        """

        observed: list[dict[str, object]] = []
        with self._session() as transaction:
            for name in server_names:
                record = self.dcim_catalog.probe_server_health(name)
                transaction.record_server_health(record)
                observed.append(
                    {
                        "server": record.server_name,
                        "status": record.status,
                        "source": record.source,
                        "freshnessSeconds": record.freshness_seconds,
                        "observedAt": record.observed_at.isoformat(),
                    }
                )
        return observed

    def detect_config_drift(self, module_id: str) -> dict[str, object]:
        """Compare what the active revision says with what deployments actually used.

        A deployment pinned to an older revision is not an error -- it is simply older.
        It becomes drift the moment someone reads the module's configuration and assumes
        that is what is running.
        """

        with self._session() as transaction:
            module = transaction.portal_module(module_id)
            if module is None:
                raise KeyError("module not found")
            active = transaction.active_config_revision(module_id)
            if module.application_id is None or active is None:
                return {"moduleId": module_id, "drifted": False, "items": []}
            deployments = self.platform.list_deployments(
                module.application_id, session=transaction
            )
            drifted = [
                {
                    "deploymentId": str(item.id),
                    "environment": item.environment.value,
                    "status": item.status.value,
                    "usedRevisionId": str(item.config_revision_id)
                    if item.config_revision_id
                    else None,
                }
                for item in deployments
                if item.status
                in {DeploymentStatus.HEALTHY, DeploymentStatus.DEPLOYING}
                and item.config_revision_id != active.id
            ]
        return {
            "moduleId": module_id,
            "activeRevisionId": str(active.id),
            "activeRevisionNumber": active.revision_number,
            "drifted": bool(drifted),
            "items": drifted,
        }

    # ------------------------------------------------- versioned configuration

    @staticmethod
    def production_config_needs_approval() -> bool:
        """Whether a revision touching production must be approved by a second person.

        On by default. A configuration revision decides which machines a release lands
        on and which credential it uses, so changing it is a production change even
        though no code moved -- the same separation of duties applies.
        """

        return os.getenv("NETCI_REQUIRE_CONFIG_APPROVAL", "true").strip().lower() not in {
            "0", "false", "no"
        }

    @staticmethod
    def _touches_production(
        current_deployment_config: list[dict[str, object]],
        new_deployment_config: list[dict[str, object]],
    ) -> bool:
        def get_prod(cfg: list[dict[str, object]]) -> dict[str, object] | None:
            for item in cfg:
                env = item.get("environment")
                env_str = env.value if isinstance(env, Environment) else str(env or "")
                if env_str == Environment.PROD.value:
                    return item
            return None

        current_prod = get_prod(current_deployment_config)
        new_prod = get_prod(new_deployment_config)
        if current_prod is None and new_prod is None:
            return False
        if (current_prod is None) != (new_prod is None):
            return True
        # A key that is absent and a key that is null mean the same thing to every
        # reader of this config. Treating them as a change would demand an approver for
        # a revision that alters nothing in production, which teaches people that the
        # approval is noise.
        return _without_nulls(current_prod) != _without_nulls(new_prod)

    def config_revisions(self, module_id: str) -> dict[str, object]:
        with self._session() as transaction:
            module = transaction.portal_module(module_id)
            if module is None:
                raise KeyError("module not found")
            active_id = module.active_config_revision_id
            return {
                "moduleId": module_id,
                "activeRevisionId": str(active_id) if active_id else None,
                "configVersion": module.config_version,
                "items": [
                    self._revision_json(item, active_id)
                    for item in transaction.config_revisions(module_id)
                ],
            }

    @staticmethod
    def _revision_json(revision: ModuleConfigRevision, active_id=None) -> dict[str, object]:
        return {
            "id": str(revision.id),
            "revisionNumber": revision.revision_number,
            "fencingToken": revision.revision_number,
            "status": revision.status.value,
            "active": active_id is not None and revision.id == active_id,
            "changeSummary": revision.change_summary,
            "createdBy": revision.created_by,
            "createdAt": revision.created_at.isoformat(),
            "approvedBy": revision.approved_by,
            "approvedAt": revision.approved_at.isoformat() if revision.approved_at else None,
            "rejectionReason": revision.rejection_reason,
            "pipelineConfig": dict(revision.pipeline_config),
            "deploymentConfig": list(revision.deployment_config),
        }

    def propose_config_revision(
        self,
        module_id: str,
        *,
        pipeline_config: dict[str, object],
        deployment_config: list[dict[str, object]],
        change_summary: str,
        expected_version: int | None = None,
        actor: str,
    ) -> dict[str, object]:
        """Write a new immutable revision. Older revisions are never edited.

        A revision that touches production is written as `pending_approval` and does not
        become active until someone else approves it. One that does not is activated
        immediately -- requiring a second person for a dev target buys nothing and
        teaches people to route around the control.
        """

        with self._session() as transaction:
            module = transaction.portal_module(module_id)
            if module is None:
                raise KeyError("module not found")
            if expected_version is not None and module.config_version != expected_version:
                raise PortalError(
                    "CONCURRENT_MODIFICATION",
                    f"module config_version {module.config_version} does not match expected version {expected_version}; re-read and try again",
                    409,
                )
            existing = transaction.config_revisions(module_id)
            next_number = max((item.revision_number for item in existing), default=0) + 1
            needs_approval = self.production_config_needs_approval() and self._touches_production(
                list(module.deployment_config), deployment_config
            )
            revision = transaction.record_config_revision(
                ModuleConfigRevision(
                    module_id=module_id,
                    revision_number=next_number,
                    created_by=actor,
                    pipeline_config=dict(pipeline_config),
                    deployment_config=list(deployment_config),
                    change_summary=change_summary,
                    status=(
                        ConfigRevisionStatus.PENDING_APPROVAL
                        if needs_approval
                        else ConfigRevisionStatus.ACTIVE
                    ),
                )
            )
            if not needs_approval:
                self._activate(transaction, module, revision, actor)
                active_id = revision.id
            else:
                active_id = module.active_config_revision_id
            return {
                **self._revision_json(revision, active_id),
                "requiresApproval": needs_approval,
            }

    def approve_config_revision(
        self, module_id: str, revision_id: UUID | str, actor: str
    ) -> dict[str, object]:
        if isinstance(revision_id, str):
            revision_id = UUID(revision_id)
        with self._session() as transaction:
            module = transaction.portal_module(module_id)
            if module is None:
                raise KeyError("module not found")
            revision = transaction.config_revision(revision_id)
            if revision is None or revision.module_id != module_id:
                raise KeyError("configuration revision not found")
            if revision.status != ConfigRevisionStatus.PENDING_APPROVAL:
                raise PortalError(
                    "INVALID_REVISION_STATE",
                    f"revision {revision.revision_number} is {revision.status.value}, "
                    "not waiting for approval",
                    409,
                )
            if self.separation_of_duties_required() and revision.created_by == actor:
                raise PortalError(
                    "SEPARATION_OF_DUTIES",
                    "a configuration revision must be approved by someone other than its author",
                    403,
                )
            transaction.update_config_revision_status(
                revision_id,
                ConfigRevisionStatus.ACTIVE,
                approved_by=actor,
                approved_at=datetime.now(timezone.utc),
            )
            self._activate(transaction, module, revision, actor, approved_by=actor)
            transaction.record_notification(
                NotificationRecord(
                    id=uuid4(),
                    event_type="config.approved",
                    aggregate_type="config_revision",
                    aggregate_id=str(revision_id),
                    payload={
                        "module_id": module_id,
                        "revision_number": revision.revision_number,
                        "approved_by": actor,
                    },
                    recipient="events@netci.local",
                )
            )
            updated = transaction.config_revision(revision_id)
            assert updated is not None
            return self._revision_json(updated, updated.id)

    def reject_config_revision(
        self, module_id: str, revision_id: UUID | str, actor: str, reason: str
    ) -> dict[str, object]:
        if isinstance(revision_id, str):
            revision_id = UUID(revision_id)
        with self._session() as transaction:
            revision = transaction.config_revision(revision_id)
            if revision is None or revision.module_id != module_id:
                raise KeyError("configuration revision not found")
            if revision.status != ConfigRevisionStatus.PENDING_APPROVAL:
                raise PortalError(
                    "INVALID_REVISION_STATE", "revision is not waiting for approval", 409
                )
            transaction.update_config_revision_status(
                revision_id, ConfigRevisionStatus.REJECTED, rejection_reason=reason
            )
            transaction.record_notification(
                NotificationRecord(
                    id=uuid4(),
                    event_type="config.rejected",
                    aggregate_type="config_revision",
                    aggregate_id=str(revision_id),
                    payload={
                        "module_id": module_id,
                        "revision_number": revision.revision_number,
                        "rejected_by": actor,
                        "reason": reason,
                    },
                    recipient="events@netci.local",
                )
            )
            updated = transaction.config_revision(revision_id)
            assert updated is not None
            return self._revision_json(updated)

    def rollback_config_revision(
        self, module_id: str, revision_number: int, actor: str
    ) -> dict[str, object]:
        """Make an earlier revision active again by copying it forward.

        The old revision is not reactivated in place: history must stay append-only, so
        "we went back to revision 3" is itself recorded as revision 7. Otherwise the
        sequence of what was live when cannot be reconstructed.
        """

        with self._session() as transaction:
            module = transaction.portal_module(module_id)
            if module is None:
                raise KeyError("module not found")
            source = transaction.config_revision_by_number(module_id, revision_number)
            if source is None:
                raise KeyError(f"revision {revision_number} not found for module {module_id}")
            existing = transaction.config_revisions(module_id)
            next_number = max((item.revision_number for item in existing), default=0) + 1
            needs_approval = self.production_config_needs_approval() and self._touches_production(
                list(module.deployment_config), list(source.deployment_config)
            )
            revision = transaction.record_config_revision(
                ModuleConfigRevision(
                    module_id=module_id,
                    revision_number=next_number,
                    created_by=actor,
                    pipeline_config=dict(source.pipeline_config),
                    deployment_config=list(source.deployment_config),
                    change_summary=f"rollback to revision {revision_number}",
                    status=(
                        ConfigRevisionStatus.PENDING_APPROVAL
                        if needs_approval
                        else ConfigRevisionStatus.ACTIVE
                    ),
                )
            )
            if not needs_approval:
                self._activate(transaction, module, revision, actor)
                active_id = revision.id
            else:
                active_id = module.active_config_revision_id
            return {
                **self._revision_json(revision, active_id),
                "rolledBackTo": revision_number,
                "requiresApproval": needs_approval,
            }

    def apply_config_revision(
        self,
        module_id: str,
        *,
        environment: str,
        revision_id: UUID | str | None = None,
        fencing_token: int | None = None,
        actor: str,
    ) -> dict[str, object]:
        """Fast-track deploy updated configuration without re-running CI.
        Reuses the existing immutable artifact digest for the specified environment."""
        if isinstance(revision_id, str):
            revision_id = UUID(revision_id)
        with self._session() as transaction:
            module = transaction.portal_module(module_id)
            if module is None:
                raise KeyError(f"module {module_id} not found")
            application_id = module.application_id
            if application_id is None:
                raise PortalError("MODULE_NOT_PROVISIONED", "module has no delivery application", 409)

            if revision_id is not None:
                revision = transaction.config_revision(revision_id)
                if revision is None or revision.module_id != module_id:
                    raise KeyError("configuration revision not found")
            else:
                active_id = module.active_config_revision_id
                if not active_id:
                    raise PortalError("NO_ACTIVE_REVISION", "module has no active configuration revision", 404)
                revision = transaction.config_revision(active_id)
                if revision is None:
                    raise KeyError("active configuration revision not found")

            # Fencing token validation for stale writers and delayed worker execution
            if fencing_token is not None:
                if fencing_token != revision.revision_number:
                    raise PortalError(
                        "STALE_FENCING_TOKEN",
                        f"Fencing token {fencing_token} does not match revision token {revision.revision_number}. Delayed worker apply rejected.",
                        409,
                    )

            if revision.status == ConfigRevisionStatus.SUPERSEDED:
                raise PortalError(
                    "SUPERSEDED_FENCING_TOKEN",
                    f"Cannot apply superseded configuration revision {revision.revision_number}.",
                    409,
                )

            # Risk Classification Guardrail
            risk_level, risk_reasons = classify_config_risk(
                revision.pipeline_config,
                revision.deployment_config,
                target_env=environment,
            )

            if risk_level == "critical":
                raise PortalError(
                    "FAST_APPLY_PROHIBITED_CRITICAL_RISK",
                    f"Fast-track apply is prohibited for CRITICAL risk configuration ({'; '.join(risk_reasons)}). "
                    "Changes affecting database schema, credentials, or encryption must go through full progressive deployment pipeline.",
                    422,
                )

            if risk_level == "high" and environment.lower() == "prod":
                # High-risk changes on production require pre-approved revision by someone other than author
                if revision.status != ConfigRevisionStatus.ACTIVE or not revision.approved_by:
                    raise PortalError(
                        "DUAL_CONTROL_REQUIRED",
                        f"High-risk production configuration change requires dual-control approval before fast-apply ({'; '.join(risk_reasons)}).",
                        403,
                    )

            # Which real artifact is this configuration being applied to? The most recent
            # deployment in this environment, else the most recent succeeded run. Nothing
            # else: this used to fall through to a digest hashed from the module's name,
            # which produced a "healthy" deployment of an artifact that did not exist.
            target_env = Environment(environment.lower())
            source_run_id: UUID | None = None
            # What the environment is serving now (after a rollback, the restored digest),
            # and the run that built it. Found by the live stack: the newest deployment
            # record after a rollback is the rolled-back one, whose run is `rolled_back`.
            in_service = self.platform.source_run_in_service(application_id, target_env)
            if in_service is not None:
                source_run_id = in_service.id
            if source_run_id is None:
                succeeded = [
                    r for r in transaction.pipeline_runs(application_id=application_id)
                    if r.status == PipelineStatus.SUCCEEDED and r.artifact_digest
                ]
                if succeeded:
                    source_run_id = max(succeeded, key=lambda r: r.created_at).id
            if source_run_id is None:
                raise PortalError(
                    "NO_DEPLOYABLE_ARTIFACT",
                    f"module {module_id} has no built artifact for {environment}; run a "
                    "pipeline first -- configuration cannot be applied to nothing",
                    409,
                )
            managed = self._delivery_parameters_from_revision(revision, target_env, module_id)

        # The deployment itself goes through the delivery domain: lease, state machine,
        # workflow, and a health result reported by the worker -- not asserted here.
        deployment = self.platform.redeploy_artifact(
            application_id,
            environment=target_env,
            source_pipeline_run_id=source_run_id,
            config_revision_id=revision.id,
            actor=actor,
            parameters=managed,
            reason=f"configuration revision {revision.revision_number} ({risk_level} risk)",
        )
        return {
            "deploymentId": str(deployment.id),
            "moduleId": module_id,
            "environment": environment,
            "revisionNumber": revision.revision_number,
            "status": deployment.status.value,
            "artifactDigest": deployment.artifact_digest,
            "sourcePipelineRunId": str(source_run_id),
            "fencingToken": deployment.fencing_token,
            "configBypassedCi": True,
            "riskLevel": risk_level,
            "riskReasons": risk_reasons,
            "message": (
                f"Configuration revision #{revision.revision_number} ({risk_level.upper()} risk) "
                f"is being applied to {environment} as deployment {deployment.id}; "
                f"status is {deployment.status.value} until the worker reports."
            ),
        }

    def _delivery_parameters_from_revision(
        self, revision: ModuleConfigRevision, environment: Environment, module_id: str
    ) -> dict[str, object]:
        """Server-managed deployment parameters, from the revision being applied."""

        target = next(
            (
                item for item in revision.deployment_config
                if (
                    item.get("environment").value
                    if isinstance(item.get("environment"), Environment)
                    else str(item.get("environment"))
                ) == environment.value
            ),
            None,
        )
        if target is None:
            raise PortalError(
                "DEPLOYMENT_TARGET_NOT_CONFIGURED",
                f"revision {revision.revision_number} has no {environment.value} target",
                409,
            )
        managed: dict[str, object] = {
            **runtime_parameters(target),
            "app_name": module_id,
            "target_environment": environment.value,
            "target_hosts": list(target.get("servers") or []),
            "deployment_tasks": [],
            "task_settings": {},
            "runtime_health_verified": True,
        }
        namespace = str(target.get("namespace") or "").strip()
        kubeconfig_ref = str(target.get("kubeconfigRef") or "").strip()
        if namespace:
            managed["target_namespace"] = namespace
        if kubeconfig_ref:
            managed["kubeconfig_ref"] = kubeconfig_ref
        return managed

    def diff_config_revisions(
        self, module_id: str, left: int, right: int
    ) -> dict[str, object]:
        """What changed between two revisions, field by field."""

        with self._session() as transaction:
            a = transaction.config_revision_by_number(module_id, left)
            b = transaction.config_revision_by_number(module_id, right)
            if a is None or b is None:
                raise KeyError("one of the revisions does not exist")
        changes: list[dict[str, object]] = []
        for key in sorted(set(a.pipeline_config) | set(b.pipeline_config)):
            if a.pipeline_config.get(key) != b.pipeline_config.get(key):
                changes.append({
                    "path": f"pipelineConfig.{key}",
                    "from": a.pipeline_config.get(key),
                    "to": b.pipeline_config.get(key),
                })
        targets_a = {str(item.get("environment")): item for item in a.deployment_config}
        targets_b = {str(item.get("environment")): item for item in b.deployment_config}
        for environment in sorted(set(targets_a) | set(targets_b)):
            if targets_a.get(environment) != targets_b.get(environment):
                changes.append({
                    "path": f"deploymentConfig.{environment}",
                    "from": targets_a.get(environment),
                    "to": targets_b.get(environment),
                })
        return {
            "moduleId": module_id,
            "from": left,
            "to": right,
            "changeCount": len(changes),
            "changes": changes,
        }

    def detect_drift(self, module_id: str) -> dict[str, object]:
        """Compare desired configuration against observed running deployments and DCIM server status."""
        with self._session() as transaction:
            module = transaction.portal_module(module_id)
            if module is None:
                raise KeyError("module not found")
            active_rev = transaction.active_config_revision(module_id)
            active_id = str(active_rev.id) if active_rev else None
            app_id = module.application_id

            # 1. Deployment revision drift. Only a deployment that reached the target says
            # what is running there: a failed or still-deploying one changed nothing, and
            # reporting it as "running revision none" was a drift that did not exist.
            deployments = list(self.platform.list_deployments(app_id, session=transaction)) if app_id else []
            observed = {DeploymentStatus.HEALTHY, DeploymentStatus.ROLLED_BACK, DeploymentStatus.ROLLBACK_FAILED}
            latest_by_env: dict[str, Any] = {}
            for d in deployments:
                if d.status not in observed:
                    continue
                env = d.environment.value
                if env not in latest_by_env or d.created_at > latest_by_env[env].created_at:
                    latest_by_env[env] = d

            deployment_drifts = []
            for env, dep in latest_by_env.items():
                dep_rev_id = str(dep.config_revision_id) if dep.config_revision_id else None
                if active_id and dep_rev_id != active_id:
                    deployment_drifts.append({
                        "environment": env,
                        "deploymentId": str(dep.id),
                        "status": dep.status.value,
                        "runningConfigRevisionId": dep_rev_id,
                        "desiredConfigRevisionId": active_id,
                        "drifted": True,
                        "reason": f"running deployment pinned to revision {dep_rev_id or 'none'}, desired is {active_id}",
                    })
                else:
                    deployment_drifts.append({
                        "environment": env,
                        "deploymentId": str(dep.id),
                        "status": dep.status.value,
                        "runningConfigRevisionId": dep_rev_id,
                        "desiredConfigRevisionId": active_id,
                        "drifted": False,
                    })

            # 2. DCIM server status drift
            dcim_drifts = []
            for target in module.deployment_config:
                env = str(target.get("environment") or "")
                servers = list(target.get("servers") or [])
                for server in servers:
                    validation = self.dcim_catalog.validate_target(module.system_id, module.id, env, server)
                    if not validation.valid:
                        dcim_drifts.append({
                            "environment": env,
                            "server": server,
                            "dcimStatus": validation.status,
                            "message": validation.message,
                            "drifted": True,
                        })
                    else:
                        dcim_drifts.append({
                            "environment": env,
                            "server": server,
                            "dcimStatus": validation.status,
                            "message": validation.message,
                            "drifted": False,
                        })

            has_drift = any(item.get("drifted") for item in deployment_drifts) or any(item.get("drifted") for item in dcim_drifts)
            return {
                "moduleId": module_id,
                "activeRevisionId": active_id,
                "configVersion": module.config_version,
                "hasDrift": has_drift,
                "deploymentDrift": deployment_drifts,
                "dcimDrift": dcim_drifts,
            }

    def _activate(
        self,
        transaction: PlatformSession,
        module: ModuleRow,
        revision: ModuleConfigRevision,
        actor: str,
        approved_by: str | None = None,
    ) -> None:
        """Point the module at a revision, and copy it into the module's live columns.

        Compare-and-set on `config_version`: two people editing the same module
        concurrently must not both believe they won, and the loser is told so rather
        than silently overwritten.
        """

        moved = transaction.set_module_active_revision(
            module.id, revision.id, module.config_version
        )
        if not moved:
            raise PortalError(
                "CONCURRENT_MODIFICATION",
                "this module's configuration changed while your revision was being written; "
                "re-read it and try again",
                409,
            )
        transaction.replace_portal_module_config(
            module.id,
            deployment_config=list(revision.deployment_config),
            pipeline_config=dict(revision.pipeline_config),
        )
        # The revision this one replaces is history now. Leaving it `active` showed
        # three "active" revisions in the browser, and let fast-apply's superseded
        # check never fire.
        previous = module.active_config_revision_id
        if previous is not None and previous != revision.id:
            transaction.update_config_revision_status(previous, ConfigRevisionStatus.SUPERSEDED)

    @staticmethod
    def separation_of_duties_required() -> bool:
        return os.getenv("NETCI_REQUIRE_SEPARATION_OF_DUTIES", "true").strip().lower() not in {
            "0", "false", "no"
        }

    def module_for_application(self, application_id: UUID) -> dict[str, object] | None:
        """The module bound to a delivery application, if the Portal knows one.

        Onboarding retries use this: the idempotency record replays the application, and
        the module that was written in the same transaction is found from it.
        """

        with self._session() as transaction:
            module = transaction.portal_module_for_application(application_id)
            if module is None:
                return None
            return self._module(transaction, module.id)

    # -------------------------------------------------------------------- reads

    def systems(self, application_ids: set[UUID] | None = None) -> list[dict[str, object]]:
        with self._session() as transaction:
            output: list[dict[str, object]] = []
            for item in transaction.portal_systems():
                module_ids = [module.id for module in transaction.portal_modules(item.id)]
                visible = self._system(transaction, item.id, application_ids)
                if application_ids is None or not module_ids or visible["modules"]:
                    output.append(visible)
            return output

    def system(self, system_id: str, application_ids: set[UUID] | None = None) -> dict[str, object]:
        with self._session() as transaction:
            return self._system(transaction, system_id, application_ids)

    def _system(
        self,
        transaction: PlatformSession,
        system_id: str,
        application_ids: set[UUID] | None = None,
    ) -> dict[str, object]:
        item = transaction.portal_system(system_id)
        if item is None:
            raise KeyError("system not found")
        modules = [
            module
            for module in transaction.portal_modules(system_id)
            if application_ids is None or module.application_id in application_ids
        ]
        module_runs = {
            module.id: self._module_runs(transaction, module) for module in modules
        }
        runs = sum(len(found) for found in module_runs.values())
        failed = sum(
            1 for found in module_runs.values() for run in found if run.status.value == "failed"
        )
        latest_deployments = {}
        for module in modules:
            if not module.application_id:
                continue
            for deployment in self.platform.list_deployments(
                module.application_id, session=transaction
            ):
                latest_deployments[(module.application_id, deployment.environment)] = deployment
        deployment_states = [deployment.status.value for deployment in latest_deployments.values()]
        latest_runs = [found[-1] for found in module_runs.values() if found]
        if any(state == "failed" for state in deployment_states):
            system_status = "critical"
        elif any(state in {"deploying", "pending_approval"} for state in deployment_states):
            system_status = "degraded"
        elif any(state in {"healthy", "rolled_back"} for state in deployment_states):
            system_status = "healthy"
        elif latest_runs:
            # A queued/running/failed CI run is delivery activity, not runtime-health
            # evidence. It may make the delivery surface degraded, but a successful build
            # on its own can never turn a system green.
            system_status = (
                "degraded"
                if any(
                    run.status.value in {"queued", "running", "waiting_approval", "failed"}
                    for run in latest_runs
                )
                else "unknown"
            )
        else:
            system_status = "unknown"
        return {
            "id": item.id,
            "unit": item.unit,
            "description": item.description,
            "owner": item.owner,
            "status": system_status,
            "moduleCount": len(modules),
            "pipelineRuns": runs,
            "failedRuns": failed,
            "modules": [self._module(transaction, module.id) for module in modules],
        }

    def module(
        self, module_id: str, session: PlatformSession | None = None
    ) -> dict[str, object]:
        with self._session(session) as transaction:
            return self._module(transaction, module_id)

    def _module(self, transaction: PlatformSession, module_id: str) -> dict[str, object]:
        item = transaction.portal_module(module_id)
        if item is None:
            raise KeyError("module not found")
        runs = self._module_runs(transaction, item)
        application = (
            transaction.application(item.application_id) if item.application_id else None
        )
        deployments = (
            self.platform.list_deployments(item.application_id, session=transaction)
            if item.application_id
            else ()
        )
        return {
            "id": item.id,
            "systemId": item.system_id,
            "name": item.name,
            "type": item.module_type,
            "description": item.description,
            "runtime": item.runtime,
            "applicationId": str(item.application_id) if item.application_id else None,
            "ownerTeam": application.owner_team if application else None,
            "repositoryUrl": application.repository_url if application else None,
            "pipelineTemplate": application.pipeline_template if application else None,
            "versions": [row.version for row in transaction.portal_versions(module_id)],
            "activeConfigRevisionId": (
                str(item.active_config_revision_id) if item.active_config_revision_id else None
            ),
            "configVersion": item.config_version,
            "deploymentEnvironments": list(item.deployment_config),
            "pipelineConfig": dict(item.pipeline_config),
            "environments": [
                {
                    "name": environment.value,
                    "status": self._environment_status(deployments, environment),
                }
                for environment in Environment
            ],
            "pipelineRuns": [self._run_json(run) for run in runs],
            "dora": self._dora(transaction, item),
        }

    def delivery_parameters(
        self,
        module_id: str,
        environment: Environment,
        supplied: dict[str, object] | None = None,
        session: PlatformSession | None = None,
    ) -> dict[str, object]:
        """Bind a run to the module's server-owned target configuration.

        Callers may provide build inputs, but they may not replace target hosts,
        namespaces or credential references selected during module registration. This
        also prevents a production promotion from inheriting the source environment's
        staging target.
        """

        with self._session(session) as transaction:
            return self._delivery_parameters(transaction, module_id, environment, supplied)

    def _delivery_parameters(
        self,
        transaction: PlatformSession,
        module_id: str,
        environment: Environment,
        supplied: dict[str, object] | None = None,
    ) -> dict[str, object]:
        module = transaction.portal_module(module_id)
        if module is None:
            raise KeyError("module not found")
        target = next(
            (
                item
                for item in module.deployment_config
                if (
                    item.get("environment").value
                    if isinstance(item.get("environment"), Environment)
                    else str(item.get("environment"))
                ) == environment.value
            ),
            None,
        )
        if target is None:
            raise PortalError(
                "DEPLOYMENT_TARGET_NOT_CONFIGURED",
                f"module {module_id} has no {environment.value} deployment target",
                409,
            )

        configured_servers = list(target.get("servers") or [])
        # Dynamic inventory resolution: if DCIM can resolve inventory for this module/env, use it or validate configured servers
        # Revalidate each target host against DCIM immediately before dispatching
        for srv in configured_servers:
            val = self.dcim_catalog.validate_target(module.system_id, module.id, environment.value, srv)
            if not val.valid and val.status != "unconfigured":
                raise PortalError(
                    "DCIM_TARGET_UNAVAILABLE",
                    f"deployment target '{srv}' is rejected by DCIM ({val.status}): {val.message}",
                    422,
                )

        # If dynamic inventory is available and no servers configured, resolve from DCIM
        if not configured_servers:
            dynamic = self.dcim_catalog.resolve_inventory(module.system_id, module.id, environment.value)
            if dynamic:
                configured_servers = dynamic
        if str(module.runtime) in {Runtime.KUBERNETES.value, str(Runtime.KUBERNETES)}:
            # A Kubernetes target is a namespace reached through a kubeconfig, and the
            # playbook runs on the worker. Cluster nodes DCIM knows are inventory facts,
            # not Ansible hosts: passing them as `--limit` against a `hosts: localhost`
            # play would select nothing and the deployment would silently do nothing.
            configured_servers = []

        managed: dict[str, object] = {
            **runtime_parameters(target),
            "app_name": module.id,
            "target_environment": environment.value,
            "target_hosts": configured_servers,
            # Browser/API compatibility fields are deliberately neutralized. Runtime
            # commands come from reviewed playbooks, never arbitrary request metadata.
            "deployment_tasks": [],
            "task_settings": {},
            # Every checked-in runtime playbook is atomic and contains its own health
            # gate. A successful deploy activity therefore already proves runtime health.
            "runtime_health_verified": True,
        }
        namespace = str(target.get("namespace") or "").strip()
        kubeconfig_ref = str(target.get("kubeconfigRef") or "").strip()
        if namespace:
            managed["target_namespace"] = namespace
        if kubeconfig_ref:
            managed["kubeconfig_ref"] = kubeconfig_ref
        return {**dict(supplied or {}), **managed}

    def dashboard(self, application_ids: set[UUID] | None = None) -> dict[str, object]:
        with self._session() as transaction:
            systems = []
            for item in transaction.portal_systems():
                module_ids = [module.id for module in transaction.portal_modules(item.id)]
                visible = self._system(transaction, item.id, application_ids)
                if application_ids is None or not module_ids or visible["modules"]:
                    systems.append(visible)
            total_runs = sum(int(item["pipelineRuns"]) for item in systems)
            failed_runs = sum(int(item["failedRuns"]) for item in systems)
            successful_runs = max(total_runs - failed_runs, 0)
            return {
                "kpis": {
                    "systems": len(systems),
                    "modules": sum(int(item["moduleCount"]) for item in systems),
                    "pipelineRuns": total_runs,
                    "successRate": round(successful_runs / total_runs * 100, 1) if total_runs else 0,
                    "failureRate": round(failed_runs / total_runs * 100, 1) if total_runs else 0,
                },
                "pipelineActivity": self._activity_by_day(transaction, application_ids),
                "systems": systems,
            }

    def module_overview(self, module_id: str) -> dict[str, object]:
        with self._session() as transaction:
            module = self._module(transaction, module_id)
            record = transaction.portal_module(module_id)
            assert record is not None
            deployments = (
                list(self.platform.list_deployments(record.application_id, session=transaction))
                if record.application_id
                else []
            )
            versions = {row.version: row.metadata for row in transaction.portal_versions(module_id)}
            recent_releases = []
            for version in module["versions"][:3]:
                metadata = versions.get(str(version), {})
                digest = metadata.get("artifactDigest")
                matching = [item for item in deployments if item.artifact_digest == digest]
                recent_releases.append(
                    {
                        "version": version,
                        "status": matching[-1].status.value if matching else "not_deployed",
                        "testStatus": (metadata.get("ciReport") or {}).get("autoTest", "not_available"),
                    }
                )
            reports = [
                versions[str(version)]["ciReport"]
                for version in module["versions"]
                if isinstance(versions.get(str(version), {}).get("ciReport"), dict)
            ]
            # What a person opening the module wants first: what each environment is
            # running now, the last few builds and releases (newest first), and the
            # evidence behind the newest artifact -- not every deployment ever, oldest
            # first, with no digest or time on it.
            runs = list(self._module_runs(transaction, record)) if record.application_id else []
            runs_by_id = {run.id: run for run in runs}
            version_by_digest = {
                str(meta.get("artifactDigest")): tag for tag, meta in versions.items() if meta.get("artifactDigest")
            }
            configured = [str(t.get("environment")) for t in record.deployment_config] or ["dev", "staging", "prod"]
            environments = []
            for env_name in dict.fromkeys(configured):
                latest = next((d for d in reversed(deployments) if d.environment.value == env_name), None)
                if latest is None:
                    environments.append({"environment": env_name, "status": "never_deployed"})
                    continue
                run = runs_by_id.get(latest.pipeline_run_id) if latest.pipeline_run_id else None
                environments.append({
                    "environment": env_name,
                    "status": latest.status.value,
                    "deploymentId": str(latest.id),
                    "artifactDigest": latest.artifact_digest,
                    "version": version_by_digest.get(str(latest.artifact_digest)),
                    "pipelineRunId": str(latest.pipeline_run_id) if latest.pipeline_run_id else None,
                    "commitSha": run.commit_sha if run else None,
                    "strategy": latest.strategy,
                    "approvedBy": latest.approved_by,
                    "updatedAt": latest.updated_at.isoformat(),
                })

            newest_runs = sorted(runs, key=lambda r: r.created_at, reverse=True)[:6]
            newest_deployments = sorted(deployments, key=lambda d: d.created_at, reverse=True)[:6]

            # The evidence behind the newest artifact: what the pipeline actually
            # produced and verified, read from the security evidence it recorded.
            quality: dict[str, object] = {"source": None}
            for run in sorted(runs, key=lambda r: r.created_at, reverse=True):
                if run.status.value != "succeeded" or not run.artifact_digest:
                    continue
                evidence = transaction.security_evidence(run.id) or {}
                scan = evidence.get("vulnerabilityScan") if isinstance(evidence.get("vulnerabilityScan"), dict) else {}
                signature = evidence.get("signature") if isinstance(evidence.get("signature"), dict) else {}
                sbom = evidence.get("sbom") if isinstance(evidence.get("sbom"), dict) else {}
                quality = {
                    "source": {"pipelineRunId": str(run.id), "commitSha": run.commit_sha, "artifactDigest": run.artifact_digest, "at": run.updated_at.isoformat()},
                    "decision": evidence.get("decision"),
                    "sbom": {"present": bool(sbom), "format": sbom.get("format"), "generatedBy": sbom.get("generatedBy")},
                    "scan": {"scanner": scan.get("scanner"), "status": scan.get("status"), "critical": scan.get("critical"), "high": scan.get("high")},
                    "signature": {"provider": signature.get("provider"), "verified": bool(signature.get("verified"))},
                    "ciReport": evidence.get("ciReport") if isinstance(evidence.get("ciReport"), dict) else None,
                }
                break
            # The newest registered version's report, else the newest build's: a module
            # whose builds carry test results has a report before anyone tags a version.
            latest_report = reports[0] if reports else (quality.get("ciReport") or {})

            return {
                "module": module,
                "mergeRequests": [],
                "deployments": [
                    {"environment": item.environment.value, "status": item.status.value}
                    for item in deployments
                ],
                "environments": environments,
                "recentRuns": [self._run_json(run) for run in newest_runs],
                "recentDeployments": [
                    {
                        "id": str(d.id), "environment": d.environment.value, "status": d.status.value,
                        "artifactDigest": d.artifact_digest, "version": version_by_digest.get(str(d.artifact_digest)),
                        "strategy": d.strategy, "approvedBy": d.approved_by, "createdAt": d.created_at.isoformat(),
                        "updatedAt": d.updated_at.isoformat(), "pipelineRunId": str(d.pipeline_run_id) if d.pipeline_run_id else None,
                    }
                    for d in newest_deployments
                ],
                "quality": quality,
                "recentReleases": recent_releases,
                "trends": {
                    "testCoverage": latest_report.get("coveragePercentage", latest_report.get("coverage")),
                    "automationPassRate": latest_report.get("automationPassRate", 100.0 if latest_report.get("autoTest") == "passed" else None),
                    "securityFindings": latest_report.get("securityFindings"),
                },
            }

    def pipeline_runs(self, module_id: str) -> dict[str, object]:
        with self._session() as transaction:
            module = transaction.portal_module(module_id)
            if module is None:
                raise KeyError("module not found")
            return {
                "moduleId": module_id,
                "items": [
                    self._run_json(run) for run in self._module_runs(transaction, module)
                ],
            }

    def versions(self, module_id: str) -> dict[str, object]:
        with self._session() as transaction:
            module = transaction.portal_module(module_id)
            if module is None:
                raise KeyError("module not found")
            deployments = (
                self.platform.list_deployments(module.application_id, session=transaction)
                if module.application_id
                else ()
            )
            return {
                "moduleId": module_id,
                "items": [
                    {
                        "version": row.version,
                        "artifactDigest": None,
                        "signed": False,
                        "sbom": "not_available",
                        "scan": "not_available",
                        "environments": {
                            environment.value: next(
                                (
                                    deployment.status.value
                                    for deployment in reversed(deployments)
                                    if deployment.environment == environment
                                    and deployment.artifact_digest
                                    == row.metadata.get("artifactDigest")
                                ),
                                "not_deployed",
                            )
                            for environment in Environment
                        },
                        **row.metadata,
                    }
                    for row in transaction.portal_versions(module_id)
                ],
            }

    def register_version(
        self,
        module_id: str,
        *,
        tag: str,
        git_tag_url: str,
        artifact_url: str,
        pipeline_run_id: UUID | None,
        artifact_digest: str | None,
        created_by: str = "Admin",
    ) -> dict[str, object]:
        with self._session() as transaction:
            module = transaction.portal_module(module_id)
            if module is None:
                raise KeyError("module not found")
            existing = transaction.portal_version(module_id, tag)
            if existing is not None:
                existing_meta = existing.metadata
                existing_digest = existing_meta.get("artifactDigest")
                existing_run = existing_meta.get("pipelineRunId")
                req_run = str(pipeline_run_id) if pipeline_run_id else None
                if existing_digest == artifact_digest and existing_run == req_run:
                    return {"moduleId": module_id, "version": tag, **existing_meta}
                raise ValueError(
                    f"release version '{tag}' already exists with different digest or provenance"
                )
            run = None
            evidence: dict[str, object] = {}
            if pipeline_run_id is not None or artifact_digest is not None:
                if pipeline_run_id is None or artifact_digest is None:
                    raise ValueError("pipelineRunId and artifactDigest must be supplied together")
                if module.application_id is None:
                    raise ValueError("module has no delivery application")
                run = self.platform.get_pipeline(pipeline_run_id, session=transaction)
                if run.application_id != module.application_id:
                    raise ValueError("pipeline run belongs to another module")
                if run.status != PipelineStatus.SUCCEEDED or run.artifact_digest != artifact_digest:
                    raise ValueError(
                        "version requires a successful pipeline run with the same artifact digest"
                    )
                evidence = self.platform.security_evidence(run.id, session=transaction)
                if evidence.get("decision") != "allow" or evidence.get("artifactDigest") != artifact_digest:
                    raise ValueError(
                        "version requires allowed security evidence for the same artifact digest"
                    )
            record = {
                "gitTagUrl": git_tag_url,
                "artifactUrl": artifact_url,
                "pipelineRunId": str(run.id) if run else None,
                "artifactDigest": artifact_digest,
                "promotable": run is not None,
                "signed": bool((evidence.get("signature") or {}).get("verified")),
                "sbom": "available" if evidence.get("sbom") else "not_available",
                "scan": (evidence.get("vulnerabilityScan") or {}).get("status", "not_available"),
                "createdBy": created_by,
                "createdAt": datetime.now(timezone.utc).isoformat(),
                # The run's test result, if the pipeline recorded one, is this version's
                # automation evidence from the start; a per-tag report may still replace it.
                "ciReport": (
                    {**evidence["ciReport"], "source": "pipeline-run"}
                    if isinstance(evidence.get("ciReport"), dict) and evidence["ciReport"].get("autoTest") else None
                ),
            }
            try:
                transaction.insert_portal_version(VersionRow(module_id, tag, record))
            except VersionConflict:
                rechecked = transaction.portal_version(module_id, tag)
                if rechecked is not None:
                    re_meta = rechecked.metadata
                    if re_meta.get("artifactDigest") == artifact_digest and re_meta.get("pipelineRunId") == (str(run.id) if run else None):
                        return {"moduleId": module_id, "version": tag, **re_meta}
                raise ValueError(
                    f"release version '{tag}' already exists with different digest or provenance"
                )

            audit_unit = UnitOfWork(
                audit=[
                    AuditRecord(
                        event_type="release_version.created",
                        application_id=module.application_id,
                        pipeline_run_id=run.id if run else None,
                        actor=created_by,
                        payload={
                            "moduleId": module_id,
                            "version": tag,
                            "artifactDigest": artifact_digest,
                            "createdBy": created_by,
                            "sourceRunId": str(run.id) if run else None,
                        },
                    )
                ]
            )
            transaction.apply(audit_unit)
            return {"moduleId": module_id, "version": tag, **record}

    def record_ci_report(self, module_id: str, tag: str, report: dict[str, object]) -> dict[str, object]:
        with self._session() as transaction:
            module = transaction.portal_module(module_id)
            if module is None:
                raise KeyError("module not found")
            existing = transaction.portal_version(module_id, tag)
            if existing is None:
                stub = {
                    "gitTagUrl": None,
                    "artifactUrl": None,
                    "createdBy": "netCI Pipeline",
                    "createdAt": datetime.now(timezone.utc).isoformat(),
                }
                transaction.insert_portal_version(VersionRow(module_id, tag, stub))
            transaction.insert_version_ci_report(module_id, tag, report)
            audit_unit = UnitOfWork(
                audit=[
                    AuditRecord(
                        event_type="release_version.ci_report_recorded",
                        application_id=module.application_id,
                        actor="netCI Pipeline",
                        payload={"moduleId": module_id, "version": tag, "report": report},
                    )
                ]
            )
            transaction.apply(audit_unit)
            return {"moduleId": module_id, "version": tag, **dict(report)}

    def dora(self, scope_id: str) -> dict[str, object]:
        with self._session() as transaction:
            module = transaction.portal_module(scope_id)
            if module is not None:
                projection = self._dora_projection(
                    transaction, [module.application_id] if module.application_id else []
                )
                return {"scope": "module", "scopeId": scope_id, **projection}
            if transaction.portal_system(scope_id) is not None:
                # A system aggregates its modules' event streams rather than averaging
                # their metrics -- averaging rates would weight a quiet module equally.
                application_ids = [
                    item.application_id
                    for item in transaction.portal_modules(scope_id)
                    if item.application_id
                ]
                return {
                    "scope": "system",
                    "scopeId": scope_id,
                    **self._dora_projection(transaction, application_ids),
                }
            raise KeyError("scope not found")

    # ------------------------------------------------------- production requests

    def production_requests(self) -> list[dict[str, object]]:
        with self._session() as transaction:
            return self._production_requests(transaction)

    def _production_requests(self, transaction: PlatformSession) -> list[dict[str, object]]:
        return [self._request_json(transaction, row) for row in transaction.portal_requests()]

    @staticmethod
    def _request_json(transaction: PlatformSession, request: RequestRow) -> dict[str, object]:
        modules = []
        for member in sorted(request.modules, key=lambda item: item.deployment_order):
            module = transaction.portal_module(member.module_id)
            modules.append(
                {
                    "moduleId": member.module_id,
                    "moduleName": module.name if module else member.module_id,
                    "systemId": module.system_id if module else None,
                    "version": member.version,
                    "deploymentOrder": member.deployment_order,
                    "dependencies": list(member.dependencies),
                    "status": member.status,
                    "deploymentId": str(member.deployment_id) if member.deployment_id else None,
                    "startedAt": member.started_at.isoformat() if member.started_at else None,
                    "completedAt": member.completed_at.isoformat() if member.completed_at else None,
                    "errorMessage": member.error_message,
                }
            )
        return {
            "id": request.id,
            "modules": modules,
            "requestedBy": request.requested_by,
            "scheduledFor": request.scheduled_for.isoformat(),
            "rollbackStrategy": request.rollback_strategy,
            "runAutomationTests": request.run_automation_tests,
            "status": request.status,
            "deploymentId": str(request.deployment_id) if request.deployment_id else None,
            "comment": request.comment,
            "releasePlan": request.release_plan,
            "strategy": request.strategy,
            "strategyConfig": request.strategy_config,
            "canaryRules": (request.strategy_config or {}).get("canary_rules") or {},
            "createdAt": request.created_at.isoformat() if request.created_at else None,
        }

    def production_request(self, request_id: str) -> dict[str, object] | None:
        """One request, or None. Used by the approval endpoint to learn who asked for it."""

        with self._session() as transaction:
            row = transaction.portal_request(request_id)
            return self._request_json(transaction, row) if row is not None else None

    def create_production_request(
        self,
        *,
        modules: list[dict[str, object]],
        requested_by: str,
        scheduled_for: datetime,
        rollback_strategy: str,
        run_automation_tests: bool,
        idempotency_key: str | None = None,
        strategy: str = "rolling",
        strategy_config: dict[str, Any] | None = None,
    ) -> dict[str, object]:
        if not modules:
            raise PortalError("EMPTY_MODULES", "at least one module must be selected", 422)

        # Validate DAG dependencies and compute execution waves
        try:
            plan = compute_dag_waves(modules)
        except DagValidationError as exc:
            raise PortalError(exc.code, str(exc), exc.status_code)

        strategy_cfg = dict(strategy_config or {})
        signature: tuple[object, ...] = (
            tuple(
                (
                    str(item["moduleId"]),
                    str(item["version"]),
                    int(item.get("deploymentOrder", 1)),
                    tuple(str(d) for d in (item.get("dependencies") or ())),
                )
                for item in modules
            ),
            requested_by,
            scheduled_for.isoformat(),
            rollback_strategy,
            run_automation_tests,
            strategy,
        )
        request_hash = hashlib.sha256(
            json.dumps(signature, separators=(",", ":"), default=str).encode()
        ).hexdigest()

        with self._session() as transaction:
            if idempotency_key:
                existing = transaction.portal_request_by_idempotency_key(idempotency_key)
                if existing is not None:
                    if existing.request_hash != request_hash:
                        raise PortalError(
                            "IDEMPOTENCY_CONFLICT",
                            "idempotency key was already used with a different request",
                            409,
                        )
                    return self._request_json(transaction, existing)

            requested_modules: list[RequestModuleRow] = []
            for item in modules:
                module_id = str(item["moduleId"])
                module = transaction.portal_module(module_id)
                if module is None:
                    raise KeyError(f"module {module_id} not found")
                version = str(item["version"])
                if transaction.portal_version(module_id, version) is None:
                    raise ValueError(f"version {version} is not registered for module {module_id}")
                self._delivery_parameters(transaction, module_id, Environment.PROD)
                deps = tuple(str(d) for d in (item.get("dependencies") or ()))
                requested_modules.append(
                    RequestModuleRow(
                        module_id=module_id,
                        version=version,
                        deployment_order=int(item.get("deploymentOrder", 1)),
                        dependencies=deps,
                        status="pending",
                    )
                )

            request = RequestRow(
                id=str(uuid4()),
                modules=tuple(requested_modules),
                requested_by=requested_by,
                scheduled_for=scheduled_for,
                rollback_strategy=rollback_strategy,
                run_automation_tests=run_automation_tests,
                status="waiting_approval",
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                release_plan=plan,
                strategy=strategy,
                strategy_config=strategy_cfg,
            )
            transaction.insert_portal_request(request)
            return self._request_json(transaction, request)

    def approve_request(self, request_id: str, actor: str, comment: str | None = None) -> dict[str, object]:
        with self._session() as transaction:
            request = transaction.portal_request(request_id)
            if request is None:
                raise KeyError("production request not found")
            if request.status != "waiting_approval":
                raise ValueError("production request is not waiting for approval")

            # Enforce Separation of Duties at domain boundary
            if (
                self.separation_of_duties_required()
                and actor
                and actor not in ("", "anonymous")
                and request.requested_by
                and request.requested_by not in ("", "anonymous")
                and request.requested_by == actor
            ):
                raise PortalError(
                    "SEPARATION_OF_DUTIES",
                    "production request must be approved by someone other than its requester",
                    403,
                )

            # Validate each module has verified release artifact and meets automation gate
            for requested in request.modules:
                version_row = transaction.portal_version(requested.module_id, requested.version)
                metadata = version_row.metadata if version_row is not None else {}
                pipeline_run_id = metadata.get("pipelineRunId")
                artifact_digest = metadata.get("artifactDigest")
                if not pipeline_run_id or not artifact_digest:
                    raise PortalError(
                        "VERSION_NOT_PROMOTABLE",
                        f"registered version {requested.version} for module {requested.module_id} is not linked to a verified pipeline artifact",
                        409,
                    )
                report = metadata.get("ciReport")
                if request.run_automation_tests and (
                    not isinstance(report, dict) or report.get("autoTest") != "passed"
                ):
                    raise PortalError(
                        "AUTOMATION_GATE_FAILED",
                        f"production request requires a passing automation-test result for module {requested.module_id}",
                        409,
                    )

            # Claim the request before anything is dispatched. The status check above
            # and this write are two statements: without the condition in the UPDATE,
            # two reviewers approving at the same moment both pass the check and both
            # start wave 1, which puts one release into production twice. Exactly one
            # caller gets True; the loser is told the same thing a late approver is told.
            if not transaction.claim_portal_request(
                request_id, from_status="waiting_approval", to_status="approved",
                comment=comment,
            ):
                raise ValueError("production request is not waiting for approval")

        # Coordinate multi-module wave execution
        coordinator = ReleasePlanCoordinator(self, self.platform)
        coordinator.start_release(request_id, actor)

        with self._session() as transaction:
            row = transaction.portal_request(request_id)
            assert row is not None
            return self._request_json(transaction, row)

    def reject_request(self, request_id: str, actor: str, comment: str | None = None) -> dict[str, object]:
        return self._set_request_status(request_id, "rejected", actor, comment)

    def record_production_deployment_cancelled(self, deployment_id: UUID, actor: str, reason: str) -> None:
        """The deployment a request created was cancelled before it ran: the request is
        `cancelled`, not blocked (nothing failed) and not rejected (nobody refused it)."""

        with self._session() as transaction:
            request = transaction.portal_request_for_deployment(deployment_id)
            if request is None or request.status not in {"approved", "waiting_approval"}:
                return
            module = next((m for m in request.modules if m.deployment_id == deployment_id), None)
            if module is not None:
                transaction.update_portal_request_module(
                    request.id, module.module_id, status="cancelled", deployment_id=deployment_id,
                    error_message=reason or None, completed_at=datetime.now(timezone.utc),
                )
            transaction.update_portal_request(
                request.id, status="cancelled", comment=f"deployment cancelled by {actor}" + (f": {reason}" if reason else ""),
            )

    def record_production_deployment_result(
        self,
        deployment_id: UUID,
        status: str,
        message: str | None,
    ) -> None:
        with self._session() as transaction:
            request = transaction.portal_request_for_deployment(deployment_id)
            if request is None:
                return
            request_id = request.id

        coordinator = ReleasePlanCoordinator(self, self.platform)
        coordinator.record_module_deployment_result(request_id, deployment_id, status, message)

    def _set_request_status(self, request_id: str, status: str, actor: str, comment: str | None) -> dict[str, object]:
        with self._session() as transaction:
            request = transaction.portal_request(request_id)
            if request is None:
                raise KeyError("production request not found")
            if request.status != "waiting_approval":
                raise ValueError("production request is not waiting for approval")
            next_comment = comment or f"{status} by {actor}"
            transaction.update_portal_request(
                request_id, status=status, comment=next_comment, deployment_id=None
            )
            updated = transaction.portal_request(request_id)
            assert updated is not None
            return self._request_json(transaction, updated)

    # --------------------------------------------------------- servers and DCIM

    def servers(self, application_ids: set[UUID] | None = None) -> list[dict[str, object]]:
        """Every deployment target the modules name, with what is *known* about it.

        The address comes from NetBox's primary IP, the state from the edge agent's
        connection row and its last telemetry, the maintenance flag from the operator.
        Anything not known is null: an earlier version invented a 10.244.x.y address
        per row, which looked like inventory and was not.
        """

        with self._session() as transaction:
            agents = {row.hostname: row for row in transaction.list_agent_connections()}
            rows: dict[str, dict[str, object]] = {}
            for module in transaction.portal_modules():
                if application_ids is not None and module.application_id not in application_ids:
                    continue
                for target in module.deployment_config:
                    environment = str(target.get("environment") or "dev")
                    for hostname in target.get("servers") or []:
                        hostname = str(hostname)
                        row = rows.get(hostname)
                        if row is None:
                            row = rows[hostname] = {
                                "id": hostname,
                                "hostname": hostname,
                                "runtime": module.runtime,
                                "usedBy": [],
                                "ipAddress": None,
                                "dcim": None,
                                "agent": None,
                                "telemetry": None,
                                "status": "unknown",
                                "kind": "configured-runtime-target",
                            }
                        row["usedBy"].append({"systemId": module.system_id, "moduleId": module.id, "environment": environment})
                        # Backwards-compatible single-valued fields: the first user.
                        row.setdefault("systemId", module.system_id)
                        row.setdefault("moduleId", module.id)
                        row.setdefault("environment", environment)
            for hostname, row in rows.items():
                first = row["usedBy"][0]
                try:
                    verdict = self.dcim_catalog.validate_target(first["systemId"], first["moduleId"], first["environment"], hostname)
                    details = verdict.details if isinstance(verdict.details, dict) else {}
                    row["dcim"] = {"status": verdict.status, "valid": verdict.valid, "message": verdict.message,
                                   "netboxUrl": details.get("netboxUrl"), "site": details.get("environment")}
                    if details.get("ipAddress"):
                        row["ipAddress"] = details["ipAddress"]
                except Exception as exc:  # noqa: BLE001 - reported per row, never invented
                    row["dcim"] = {"status": "error", "valid": False, "message": str(exc)[:200]}
                agent = agents.get(hostname)
                if agent is not None:
                    seen_seconds = (datetime.now(timezone.utc) - agent.last_seen_at).total_seconds()
                    row["agent"] = {"replicaId": agent.replica_id, "lastSeenAt": agent.last_seen_at.isoformat(), "stale": seen_seconds > 90}
                telemetry = transaction.get_server_telemetry(hostname)
                if telemetry is not None:
                    row["telemetry"] = {"cpuPercent": telemetry.cpu_percent, "memPercent": telemetry.mem_percent,
                                        "diskPercent": telemetry.disk_percent, "observedAt": telemetry.observed_at.isoformat()}
                maint = transaction.get_server_maintenance(hostname)
                if maint and maint.in_maintenance:
                    row["status"] = "maintenance"
                elif row["agent"] and not row["agent"]["stale"]:
                    row["status"] = "online"
                elif row["agent"] and row["agent"]["stale"]:
                    row["status"] = "offline"
                elif row["dcim"] and row["dcim"].get("status") in ("decommissioning", "decommissioned", "offline", "failed"):
                    row["status"] = "offline"
                else:
                    row["status"] = "unknown"
            return sorted(rows.values(), key=lambda r: str(r["hostname"]))

    def dcim_services(self, query: str) -> dict[str, object]:
        page = self.dcim_catalog.search_services(query)
        return {"source": page.source, "status": page.status, "items": page.items}

    def dcim_modules(self, system_id: str) -> dict[str, object]:
        with self._session() as transaction:
            if transaction.portal_system(system_id) is None:
                raise KeyError("system not found")
            registered_ids = {module.id for module in transaction.portal_modules(system_id)}
        page = self.dcim_catalog.list_modules(system_id)
        items = [
            {**item, "registered": str(item.get("id")) in registered_ids}
            for item in page.items
        ]
        return {"source": page.source, "status": page.status, "systemId": system_id, "items": items}

    def dcim_servers(self, system_id: str, module_id: str | None = None) -> dict[str, object]:
        with self._session() as transaction:
            if transaction.portal_system(system_id) is None:
                raise KeyError("system not found")
        page = self.dcim_catalog.list_servers(system_id, module_id)
        return {
            "source": page.source,
            "status": page.status,
            "systemId": system_id,
            "moduleId": module_id,
            "items": page.items,
        }

    def audit_events(
        self,
        *,
        system_id: str | None = None,
        module_id: str | None = None,
        application_ids: set[UUID] | None = None,
    ) -> list[dict[str, object]]:
        with self._session() as transaction:
            if module_id is not None and transaction.portal_module(module_id) is None:
                raise KeyError("module not found")
            if system_id is not None and transaction.portal_system(system_id) is None:
                raise KeyError("system not found")

            all_modules = transaction.portal_modules()
            application_to_module = {
                module.application_id: module
                for module in all_modules
                if module.application_id is not None
            }
            allowed_modules: set[str] | None = None
            if module_id is not None:
                allowed_modules = {module_id}
            elif system_id is not None:
                allowed_modules = {item.id for item in transaction.portal_modules(system_id)}

            allowed_application_ids = {
                module.application_id
                for module in all_modules
                if module.application_id is not None
                and (allowed_modules is None or module.id in allowed_modules)
            }
            if application_ids is not None:
                allowed_application_ids &= application_ids
            records = self.platform.audit_records(allowed_application_ids, session=transaction)
            return [
                {
                    "id": str(record.id),
                    "action": record.event_type,
                    "actor": record.actor or "system",
                    "target": application_to_module[record.application_id].id,
                    "applicationId": str(record.application_id),
                    "pipelineRunId": str(record.pipeline_run_id) if record.pipeline_run_id else None,
                    "deploymentId": str(record.deployment_id) if record.deployment_id else None,
                    "correlationId": record.correlation_id,
                    "details": dict(record.payload),
                    "createdAt": record.occurred_at.isoformat(),
                }
                for record in records
                if record.application_id in application_to_module
            ]

    # ------------------------------------------------------------------ helpers

    def _module_runs(self, transaction: PlatformSession, module: ModuleRow):
        if module.application_id is None:
            return ()
        return self.platform.list_pipeline_runs(module.application_id, session=transaction)

    @staticmethod
    def _run_json(run) -> dict[str, object]:
        return {
            "id": str(run.id),
            "applicationId": str(run.application_id),
            "status": run.status.value,
            "commitSha": run.commit_sha,
            "branch": run.branch,
            "environment": run.environment.value,
            "parameters": dict(run.parameters),
            "correlationId": run.correlation_id,
            "createdAt": run.created_at.isoformat(),
            "updatedAt": run.updated_at.isoformat(),
            "jenkinsRunId": run.jenkins_run_id,
            "workflowId": run.workflow_id,
            "artifactDigest": run.artifact_digest,
            # The module's run list is what the run view opens from; without the console
            # URL here "Open Jenkins" stayed disabled although the run had one.
            "consoleUrl": run.console_url,
            "retryOf": str(run.retry_of) if getattr(run, "retry_of", None) else None,
            "startedBy": run.started_by,
        }

    @staticmethod
    def _environment_status(deployments, environment: Environment) -> str:
        for deployment in reversed(deployments):
            if deployment.environment == environment:
                return deployment.status.value.replace("pending_approval", "pending").replace("rolled_back", "rolled back")
        return "not_deployed"

    def dora_projection(self, application_ids: list[UUID]) -> dict[str, object]:
        with self._session() as transaction:
            return self._dora_projection(transaction, application_ids)

    def _dora_projection(
        self, transaction: PlatformSession, application_ids: list[UUID]
    ) -> dict[str, object]:
        """Project the four DORA metrics from durable delivery events only.

        Every figure is derived from `delivery_events` rows written in the same
        transaction as the state change that produced them, so `sourceEventCount`
        is the audit trail for the numbers beside it. With no events, the metrics
        are zero -- the portal never invents a baseline.
        """

        now = datetime.now(timezone.utc)
        since = now - timedelta(days=DORA_WINDOW_DAYS)
        source: list[DeliveryEvent] = []
        for application_id in application_ids:
            source.extend(self.platform.delivery_events(application_id, session=transaction))
        windowed = [event for event in source if event.occurred_at >= since]
        projected = project_dora([self._as_dora_event(event) for event in windowed])
        weeks = DORA_WINDOW_DAYS / 7
        return {
            "metrics": [
                {
                    "key": "deploymentFrequency",
                    "label": "Deployment Frequency",
                    "value": round(float(projected["deployment_frequency"]) / weeks, 1),
                    "unit": "/wk",
                    "hint": "Production deployments per week",
                },
                {
                    "key": "leadTime",
                    "label": "Lead Time for Changes",
                    "value": round(float(projected["change_lead_time_seconds_avg"]) / 3600, 1),
                    "unit": "h",
                    "hint": "Commit to production",
                },
                {
                    "key": "changeFailureRate",
                    "label": "Change Failure Rate",
                    "value": round(float(projected["change_fail_rate"]) * 100, 1),
                    "unit": "%",
                    "hint": "Production deployments needing intervention",
                },
                {
                    "key": "timeToRestoreService",
                    "label": "Time to Restore Service",
                    "value": round(float(projected["time_to_restore_service_seconds_avg"]) / 3600, 1),
                    "unit": "h",
                    "hint": "Failure to restored service",
                },
            ],
            "sourceEventCount": len(windowed),
            "window": {"from": since.isoformat(), "to": now.isoformat(), "days": DORA_WINDOW_DAYS},
        }

    @staticmethod
    def _as_dora_event(event: DeliveryEvent) -> DoraEvent:
        return DoraEvent(
            event_type=event.event_type.value,
            application_id=str(event.application_id),
            commit_sha=event.commit_sha,
            deployment_id=str(event.deployment_id) if event.deployment_id else None,
            environment=event.environment.value if event.environment else None,
            occurred_at=event.occurred_at,
            successful=event.successful,
            requires_intervention=event.requires_intervention,
        )

    def _dora(self, transaction: PlatformSession, module: ModuleRow) -> list[dict[str, object]]:
        application_ids = [module.application_id] if module.application_id else []
        metrics = self._dora_projection(transaction, application_ids)["metrics"]
        assert isinstance(metrics, list)
        return metrics

    def _activity_by_day(
        self, transaction: PlatformSession, application_ids: set[UUID] | None = None
    ) -> list[dict[str, object]]:
        today = datetime.now(timezone.utc).date()
        all_runs = []
        for module in transaction.portal_modules():
            if module.application_id and (
                application_ids is None or module.application_id in application_ids
            ):
                all_runs.extend(
                    self.platform.list_pipeline_runs(module.application_id, session=transaction)
                )

        days = []
        for offset in range(6, -1, -1):
            day_date = today - timedelta(days=offset)
            succeeded = sum(
                1 for r in all_runs if r.created_at.date() == day_date and r.status.value == "succeeded"
            )
            failed = sum(
                1 for r in all_runs if r.created_at.date() == day_date and r.status.value == "failed"
            )
            days.append({"date": day_date.isoformat(), "succeeded": succeeded, "failed": failed})
        return days
