"""Release Portal read models and portal-facing commands.

This layer adapts the delivery domain (applications, pipeline runs and deployments)
to the System -> Module -> Release hierarchy used by the Custom Portal. It is
intentionally isolated from transport and can later be backed by PostgreSQL
projection queries without changing the HTTP response shapes.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from .adapters.dcim import DcimCatalog, build_dcim_catalog
from .delivery import DeliveryPlatform
from .domain.models import DeliveryEvent, Environment, PipelineStatus, Runtime
from .persistence import PostgresPortalStore, StillReferenced
from .projections.dora import DoraEvent, project_dora

#: Rolling window every DORA figure is computed over. Stated in the response so a
#: number on screen can never be read without the period it belongs to.
DORA_WINDOW_DAYS = 30


class PortalError(RuntimeError):
    def __init__(self, code: str, message: str, status_code: int) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


@dataclass
class PortalSystem:
    id: str
    unit: str
    description: str
    owner: str
    status: str
    module_ids: list[str] = field(default_factory=list)


@dataclass
class PortalModule:
    id: str
    system_id: str
    name: str
    module_type: str
    description: str
    runtime: Runtime
    application_id: UUID | None = None
    versions: list[str] = field(default_factory=list)
    deployment_environments: list[dict[str, object]] = field(default_factory=list)
    pipeline_config: dict[str, object] = field(default_factory=dict)


@dataclass
class PortalProductionModule:
    module_id: str
    version: str
    deployment_order: int = 1


@dataclass
class PortalProductionRequest:
    id: str
    modules: list[PortalProductionModule]
    requested_by: str
    scheduled_for: datetime
    rollback_strategy: str
    run_automation_tests: bool
    status: str
    deployment_id: UUID | None = None
    comment: str | None = None
    idempotency_key: str | None = None
    request_hash: str | None = None


class PortalReadModel:
    """Small local projection with stable response shapes for the Portal UI."""

    def __init__(
        self,
        platform: DeliveryPlatform,
        *,
        store: PostgresPortalStore | None = None,
        dcim_catalog: DcimCatalog | None = None,
    ) -> None:
        self.platform = platform
        self.store = store if store is not None else PostgresPortalStore.from_env()
        self.dcim_catalog = dcim_catalog or build_dcim_catalog()
        self._systems: dict[str, PortalSystem] = {}
        self._modules: dict[str, PortalModule] = {}
        self._requests: dict[str, PortalProductionRequest] = {}
        self._request_idempotency: dict[str, tuple[str, str]] = {}
        self._version_records: dict[tuple[str, str], dict[str, object]] = {}
        self._persistence_error: str | None = None
        self._seed()
        self._load_persistent_state()

    def reset(self) -> None:
        self._systems.clear()
        self._modules.clear()
        self._requests.clear()
        self._request_idempotency.clear()
        self._version_records.clear()
        self._seed()
        self._load_persistent_state()

    def _load_persistent_state(self) -> None:
        self._persistence_error = None
        if self.store is None:
            return
        if not self.store.bootstrap():
            self._persistence_error = getattr(self.store, "last_error", None) or "portal persistence bootstrap failed"
            return
        data = self.store.load()
        if data is None:
            self._persistence_error = getattr(self.store, "last_error", None) or "portal persistence load failed"
            return
        if not data["systems"]:
            return
        self._systems.clear()
        self._modules.clear()
        self._requests.clear()
        for row in data["systems"]:
            self._systems[str(row["id"])] = PortalSystem(
                str(row["id"]), str(row["unit"]), str(row["description"]),
                str(row["owner"]), str(row["status"]), [],
            )
        for row in data["modules"]:
            application_id = row.get("application_id")
            if application_id is not None and not isinstance(application_id, UUID):
                application_id = UUID(str(application_id))
            module = PortalModule(
                str(row["id"]), str(row["system_id"]), str(row["name"]),
                str(row["module_type"]), str(row["description"]), Runtime(str(row.get("runtime", "docker"))),
                application_id=application_id,
                deployment_environments=list(row.get("deployment_config") or []),
                pipeline_config=dict(row.get("pipeline_config") or {}),
            )
            self._modules[module.id] = module
            if module.system_id in self._systems:
                self._systems[module.system_id].module_ids.append(module.id)
        for row in data.get("versions", []):
            module_id = str(row["module_id"])
            if module_id in self._modules:
                version = str(row["version"])
                self._modules[module_id].versions.append(version)
                metadata = row.get("metadata") or {}
                if metadata:
                    self._version_records[(module_id, version)] = dict(metadata)
        persisted_modules: dict[str, list[PortalProductionModule]] = {}
        for row in data.get("request_modules", []):
            persisted_modules.setdefault(str(row["request_id"]), []).append(
                PortalProductionModule(
                    str(row["module_id"]),
                    str(row["version"]),
                    int(row["deployment_order"]),
                )
            )
        for row in data["requests"]:
            request_id = str(row["id"])
            self._requests[request_id] = PortalProductionRequest(
                id=request_id,
                modules=persisted_modules.get(
                    request_id,
                    [PortalProductionModule(str(row["module_id"]), str(row.get("version", "v0.0.0")))],
                ),
                requested_by=str(row["requested_by"]),
                scheduled_for=row.get("scheduled_for") or datetime.now(timezone.utc),
                rollback_strategy=str(row.get("rollback_strategy", "automatic")),
                run_automation_tests=bool(row.get("run_automation_tests", True)),
                status=str(row["status"]),
                deployment_id=row.get("deployment_id"),
                comment=row.get("comment"),
                idempotency_key=row.get("idempotency_key"),
                request_hash=row.get("request_hash"),
            )
            if row.get("idempotency_key"):
                self._request_idempotency[str(row["idempotency_key"])] = (
                    str(row.get("request_hash") or ""),
                    request_id,
                )

    def persistence_health(self) -> dict[str, str]:
        if self.store is None:
            return {"mode": "memory", "status": "not_configured"}
        if self._persistence_error:
            return {"mode": "postgresql", "status": "degraded", "message": self._persistence_error}
        return {"mode": "postgresql", "status": "ready"}

    def _persist(self, method: str, *args: object) -> None:
        if self.store is None:
            return
        try:
            getattr(self.store, method)(*args)
        except StillReferenced as exc:
            # Not a storage failure: the write was refused because something still points
            # at the row. 503 would send an operator to check the database when the answer
            # is "remove the production request first".
            raise PortalError("STILL_REFERENCED", str(exc), 409) from exc
        except Exception as exc:
            raise PortalError(
                "PERSISTENCE_UNAVAILABLE",
                f"cannot persist portal state: {exc}",
                503,
            ) from exc

    def _seed(self) -> None:
        if os.getenv("NETCI_DEMO_DATA", "false").strip().lower() not in {"1", "true", "yes"}:
            return
        sample_apps = [
            ("hello-container", "Hello Container", "Local container delivery application", Runtime.DOCKER, "container-ci-cd-v1"),
            ("hello-kubernetes", "Hello Kubernetes", "Local Kubernetes deployment application", Runtime.KUBERNETES, "kubernetes-ci-cd-v1"),
            ("hello-systemd-go", "Hello Systemd Go", "Local systemd service application", Runtime.SYSTEMD, "systemd-ansible-ci-cd-v1"),
        ]
        for app_id, app_name, desc, runtime, template in sample_apps:
            self._systems[app_id] = PortalSystem(
                id=app_id,
                unit="Local Infrastructure",
                description=desc,
                owner="Admin",
                status="healthy",
                module_ids=[app_id],
            )
            app = self.platform.create_application(
                name=app_id,
                repository_url=f"https://github.com/example/{app_id}",
                pipeline_template=template,
                runtime=runtime,
                default_environment=Environment.DEV,
                stages=[],
                idempotency_key=f"portal-app-{app_id}",
            )
            module = PortalModule(
                id=app_id,
                system_id=app_id,
                name=app_name,
                module_type="Backend" if runtime != Runtime.KUBERNETES else "Workload",
                description=desc,
                runtime=runtime,
                application_id=app.id,
                versions=["v1.2.0", "v1.1.0", "v1.0.0"],
                deployment_environments=[
                    {
                        "displayName": "Development",
                        "environment": "dev",
                        "runtime": runtime.value,
                        "servers": ["localhost"],
                        "tasks": ["Health check"],
                    },
                    {
                        "displayName": "Staging",
                        "environment": "staging",
                        "runtime": runtime.value,
                        "servers": [f"srv-{app_id}-staging"],
                        "tasks": ["Health check", "Smoke tests"],
                    },
                    {
                        "displayName": "Production",
                        "environment": "prod",
                        "runtime": runtime.value,
                        "servers": [f"srv-{app_id}-prod"],
                        "tasks": ["Health check", "Traffic shift"],
                    },
                ],
                pipeline_config={"runner": "local", "strategy": "Trunk-based"},
            )
            self._modules[app_id] = module

            # Seed version CI reports
            for version_tag in ["v1.2.0", "v1.1.0", "v1.0.0"]:
                self._version_records[(app_id, version_tag)] = {
                    "gitTagUrl": f"https://github.com/example/{app_id}/releases/tag/{version_tag}",
                    "artifactUrl": f"http://127.0.0.1:55000/{app_id}:{version_tag}",
                    "createdBy": "netCI Pipeline",
                    "createdAt": datetime.now(timezone.utc).isoformat(),
                    "ciReport": {
                        "testPassCount": 42,
                        "testFailCount": 0,
                        "coveragePercentage": 94.5,
                        "vulnerabilityScan": "passed",
                        "sastPassed": True,
                        "buildDurationSeconds": 48,
                    },
                }

            # Version records and module definitions are seeded cleanly above

    def create_system(self, *, system_id: str, unit: str, description: str, owner: str) -> dict[str, object]:
        if system_id in self._systems:
            raise ValueError("system already exists")
        record = PortalSystem(system_id, unit, description, owner, "unknown", [])
        self._persist(
            "insert_system",
            {"id": system_id, "unit": unit, "description": description, "owner": owner, "status": "unknown"},
        )
        self._systems[system_id] = record
        return self.system(system_id)

    def remove_system(self, system_id: str) -> None:
        """Detach a system and its modules from the Portal.

        Persisted first, then applied in memory -- the same order `DeliveryPlatform._commit`
        uses, and for the same reason: a storage failure must never leave the API reporting
        a state the database does not hold.
        """

        if system_id not in self._systems:
            raise KeyError("system not found")
        requested_module_ids = {
            item.module_id for request in self._requests.values() for item in request.modules
        }
        referenced = requested_module_ids.intersection(self._systems[system_id].module_ids)
        if referenced:
            raise PortalError(
                "STILL_REFERENCED",
                f"system {system_id} has modules referenced by production requests",
                409,
            )
        self._persist("delete_system", system_id)

        system = self._systems[system_id]
        for m_id in list(system.module_ids):
            self._modules.pop(m_id, None)
        del self._systems[system_id]

    def remove_module(self, module_id: str) -> None:
        """Detach one module from the Portal.

        Delivery history is deliberately preserved: the underlying application, its runs
        and its deployments stay, because an audit trail that disappears when someone
        tidies up the Portal is not an audit trail.
        """

        if module_id not in self._modules:
            raise KeyError("module not found")
        if any(
            item.module_id == module_id
            for request in self._requests.values()
            for item in request.modules
        ):
            raise PortalError(
                "STILL_REFERENCED",
                f"module {module_id} is still referenced by a production request",
                409,
            )
        self._persist("delete_module", module_id)

        module = self._modules[module_id]
        if module.system_id in self._systems:
            sys_mods = self._systems[module.system_id].module_ids
            if module_id in sys_mods:
                sys_mods.remove(module_id)
        del self._modules[module_id]

    def update_module(
        self,
        module_id: str,
        *,
        name: str,
        module_type: str,
        description: str,
    ) -> dict[str, object]:
        module = self._modules.get(module_id)
        if module is None:
            raise KeyError("module not found")
        self._persist("update_module", module_id, name, module_type, description)
        module.name = name
        module.module_type = module_type
        module.description = description
        return self.module(module_id)


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
    ) -> dict[str, object]:
        self.validate_module_slot(system_id, module_id)
        system = self._systems[system_id]
        module = PortalModule(
            module_id, system_id, name, module_type, description, runtime,
            application_id=application_id,
            deployment_environments=deployment_environments,
            pipeline_config=dict(pipeline_config),
        )
        self._persist(
            "insert_module",
            {"id": module_id, "system_id": system_id, "application_id": application_id, "runtime": runtime.value, "name": name, "module_type": module_type, "description": description, "deployment_config": deployment_environments, "pipeline_config": pipeline_config},
        )
        self._modules[module_id] = module
        system.module_ids.append(module_id)
        return self.module(module_id)

    def validate_module_slot(self, system_id: str, module_id: str) -> None:
        if system_id not in self._systems:
            raise KeyError("system not found")
        if module_id in self._modules:
            raise ValueError("module already exists")

    def systems(self, application_ids: set[UUID] | None = None) -> list[dict[str, object]]:
        output: list[dict[str, object]] = []
        for item in self._systems.values():
            visible = self.system(item.id, application_ids)
            if application_ids is None or not item.module_ids or visible["modules"]:
                output.append(visible)
        return output

    def system(self, system_id: str, application_ids: set[UUID] | None = None) -> dict[str, object]:
        item = self._systems.get(system_id)
        if item is None:
            raise KeyError("system not found")
        modules = [
            self._modules[module_id]
            for module_id in item.module_ids
            if module_id in self._modules
            and (
                application_ids is None
                or self._modules[module_id].application_id in application_ids
            )
        ]
        runs = sum(self._module_runs(module) for module in modules)
        failed = sum(1 for module in modules for run in self._module_runs_raw(module) if run.status.value == "failed")
        latest_deployments = {}
        for module in modules:
            if not module.application_id:
                continue
            for deployment in self.platform.list_deployments(module.application_id):
                latest_deployments[(module.application_id, deployment.environment)] = deployment
        deployment_states = [deployment.status.value for deployment in latest_deployments.values()]
        latest_runs = [runs[-1] for module in modules for runs in [self._module_runs_raw(module)] if runs]
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
                if any(run.status.value in {"queued", "running", "waiting_approval", "failed"} for run in latest_runs)
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
            "modules": [self.module(module.id) for module in modules],
        }

    def module(self, module_id: str) -> dict[str, object]:
        item = self._modules.get(module_id)
        if item is None:
            raise KeyError("module not found")
        runs = self._module_runs_raw(item)
        deployments = self.platform.list_deployments(item.application_id) if item.application_id else ()
        return {
            "id": item.id,
            "systemId": item.system_id,
            "name": item.name,
            "type": item.module_type,
            "description": item.description,
            "runtime": item.runtime.value,
            "applicationId": str(item.application_id) if item.application_id else None,
            "versions": list(item.versions),
            "deploymentEnvironments": list(item.deployment_environments),
            "pipelineConfig": dict(item.pipeline_config),
            "environments": [
                {
                    "name": environment.value,
                    "status": self._environment_status(deployments, environment),
                }
                for environment in Environment
            ],
            "pipelineRuns": [self._run_json(run) for run in runs],
            "dora": self._dora(module_id),
        }

    def delivery_parameters(
        self,
        module_id: str,
        environment: Environment,
        supplied: dict[str, object] | None = None,
    ) -> dict[str, object]:
        """Bind a run to the module's server-owned target configuration.

        Callers may provide build inputs, but they may not replace target hosts,
        namespaces or credential references selected during module registration. This
        also prevents a production promotion from inheriting the source environment's
        staging target.
        """

        module = self._modules.get(module_id)
        if module is None:
            raise KeyError("module not found")
        target = next(
            (
                item
                for item in module.deployment_environments
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

        managed: dict[str, object] = {
            "app_name": module.id,
            "target_environment": environment.value,
            "target_hosts": list(target.get("servers") or []),
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
        systems = self.systems(application_ids)
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
            "pipelineActivity": self._activity_by_day(application_ids),
            "systems": systems,
        }

    def module_overview(self, module_id: str) -> dict[str, object]:
        module = self.module(module_id)
        module_record = self._modules[module_id]
        deployments = (
            list(self.platform.list_deployments(module_record.application_id))
            if module_record.application_id
            else []
        )
        recent_releases = []
        for version in module["versions"][:3]:
            record = self._version_records.get((module_id, str(version)), {})
            digest = record.get("artifactDigest")
            matching = [item for item in deployments if item.artifact_digest == digest]
            recent_releases.append(
                {
                    "version": version,
                    "status": matching[-1].status.value if matching else "not_deployed",
                    "testStatus": (record.get("ciReport") or {}).get("autoTest", "not_available"),
                }
            )
        reports = [
            record.get("ciReport")
            for version in module["versions"]
            for record in [self._version_records.get((module_id, str(version)), {})]
            if isinstance(record.get("ciReport"), dict)
        ]
        latest_report = reports[0] if reports else {}
        return {
            "module": module,
            "mergeRequests": [],
            "deployments": [
                {"environment": item.environment.value, "status": item.status.value}
                for item in deployments
            ],
            "recentReleases": recent_releases,
            "trends": {
                "testCoverage": latest_report.get("coveragePercentage"),
                "automationPassRate": latest_report.get("automationPassRate"),
                "securityFindings": latest_report.get("securityFindings"),
            },
        }

    def pipeline_runs(self, module_id: str) -> dict[str, object]:
        module = self._modules.get(module_id)
        if module is None:
            raise KeyError("module not found")
        return {"moduleId": module_id, "items": [self._run_json(run) for run in self._module_runs_raw(module)]}

    def versions(self, module_id: str) -> dict[str, object]:
        module = self._modules.get(module_id)
        if module is None:
            raise KeyError("module not found")
        deployments = self.platform.list_deployments(module.application_id) if module.application_id else ()
        return {
            "moduleId": module_id,
            "items": [
                {
                    "version": version,
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
                                == self._version_records.get((module_id, version), {}).get("artifactDigest")
                            ),
                            "not_deployed",
                        )
                        for environment in Environment
                    },
                    **self._version_records.get((module_id, version), {}),
                }
                for version in module.versions
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
        module = self._modules.get(module_id)
        if module is None:
            raise KeyError("module not found")
        if tag in module.versions:
            raise ValueError("version already exists")
        run = None
        evidence: dict[str, object] = {}
        if pipeline_run_id is not None or artifact_digest is not None:
            if pipeline_run_id is None or artifact_digest is None:
                raise ValueError("pipelineRunId and artifactDigest must be supplied together")
            if module.application_id is None:
                raise ValueError("module has no delivery application")
            run = self.platform.get_pipeline(pipeline_run_id)
            if run.application_id != module.application_id:
                raise ValueError("pipeline run belongs to another module")
            if run.status != PipelineStatus.SUCCEEDED or run.artifact_digest != artifact_digest:
                raise ValueError("version requires a successful pipeline run with the same artifact digest")
            evidence = self.platform.security_evidence(run.id)
            if evidence.get("decision") != "allow" or evidence.get("artifactDigest") != artifact_digest:
                raise ValueError("version requires allowed security evidence for the same artifact digest")
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
            "ciReport": None,
        }
        self._persist("upsert_version", module_id, tag, record)
        module.versions.insert(0, tag)
        self._version_records[(module_id, tag)] = record
        return {"moduleId": module_id, "version": tag, **record}

    def record_ci_report(self, module_id: str, tag: str, report: dict[str, object]) -> dict[str, object]:
        module = self._modules.get(module_id)
        if module is None:
            raise KeyError("module not found")
        record = dict(
            self._version_records.get(
                (module_id, tag),
                {
                "gitTagUrl": None,
                "artifactUrl": None,
                "createdBy": "netCI Pipeline",
                "createdAt": datetime.now(timezone.utc).isoformat(),
                },
            )
        )
        record["ciReport"] = dict(report)
        self._persist("upsert_version", module_id, tag, record)
        if tag not in module.versions:
            module.versions.insert(0, tag)
        self._version_records[(module_id, tag)] = record
        return {"moduleId": module_id, "version": tag, **dict(report)}

    def dora(self, scope_id: str) -> dict[str, object]:
        if scope_id in self._modules:
            projection = self.dora_projection(self._module_application_ids(scope_id))
            return {"scope": "module", "scopeId": scope_id, **projection}
        if scope_id in self._systems:
            # A system aggregates its modules' event streams rather than averaging
            # their metrics -- averaging rates would weight a quiet module equally.
            application_ids = [
                application_id
                for module_id in self._systems[scope_id].module_ids
                for application_id in self._module_application_ids(module_id)
            ]
            return {"scope": "system", "scopeId": scope_id, **self.dora_projection(application_ids)}
        raise KeyError("scope not found")

    def production_requests(self) -> list[dict[str, object]]:
        output: list[dict[str, object]] = []
        for request in self._requests.values():
            modules = []
            for requested_module in sorted(request.modules, key=lambda item: item.deployment_order):
                module = self._modules.get(requested_module.module_id)
                modules.append(
                    {
                        "moduleId": requested_module.module_id,
                        "moduleName": module.name if module else requested_module.module_id,
                        "version": requested_module.version,
                        "deploymentOrder": requested_module.deployment_order,
                    }
                )
            output.append(
                {
                    "id": request.id,
                    "modules": modules,
                    "requestedBy": request.requested_by,
                    "scheduledFor": request.scheduled_for.isoformat(),
                    "rollbackStrategy": request.rollback_strategy,
                    "runAutomationTests": request.run_automation_tests,
                    "status": request.status,
                    "deploymentId": str(request.deployment_id) if request.deployment_id else None,
                    "comment": request.comment,
                }
            )
        return output

    def production_request(self, request_id: str) -> dict[str, object] | None:
        """One request, or None. Used by the approval endpoint to learn who asked for it.

        Reads the same projection as `production_requests()` so the two can never
        disagree about who the requester was.
        """

        return next((item for item in self.production_requests() if item["id"] == request_id), None)

    def create_production_request(
        self,
        *,
        modules: list[dict[str, object]],
        requested_by: str,
        scheduled_for: datetime,
        rollback_strategy: str,
        run_automation_tests: bool,
        idempotency_key: str | None = None,
    ) -> dict[str, object]:
        if len(modules) != 1:
            raise PortalError(
                "MULTI_MODULE_ORCHESTRATION_UNAVAILABLE",
                "production requests currently require exactly one module",
                422,
            )
        signature: tuple[object, ...] = (
            tuple((str(item["moduleId"]), str(item["version"]), int(item["deploymentOrder"])) for item in modules),
            requested_by,
            scheduled_for.isoformat(),
            rollback_strategy,
            run_automation_tests,
        )
        request_hash = hashlib.sha256(
            json.dumps(signature, separators=(",", ":"), default=str).encode()
        ).hexdigest()
        if idempotency_key and idempotency_key in self._request_idempotency:
            existing_hash, existing_id = self._request_idempotency[idempotency_key]
            if existing_hash != request_hash:
                raise PortalError("IDEMPOTENCY_CONFLICT", "idempotency key was already used with a different request", 409)
            return next(item for item in self.production_requests() if item["id"] == existing_id)

        requested_modules: list[PortalProductionModule] = []
        for item in modules:
            module_id = str(item["moduleId"])
            module = self._modules.get(module_id)
            if module is None:
                raise KeyError(f"module {module_id} not found")
            version = str(item["version"])
            if version not in module.versions:
                raise ValueError(f"version {version} is not registered for module {module_id}")
            self.delivery_parameters(module_id, Environment.PROD)
            requested_modules.append(
                PortalProductionModule(module_id, version, int(item["deploymentOrder"]))
            )

        request_id = str(uuid4())
        request = PortalProductionRequest(
            id=request_id,
            modules=requested_modules,
            requested_by=requested_by,
            scheduled_for=scheduled_for,
            rollback_strategy=rollback_strategy,
            run_automation_tests=run_automation_tests,
            status="waiting_approval",
            idempotency_key=idempotency_key,
            request_hash=request_hash,
        )
        self._persist(
            "insert_request",
            {
                "id": request.id,
                "requested_by": request.requested_by,
                "scheduled_for": request.scheduled_for,
                "rollback_strategy": request.rollback_strategy,
                "run_automation_tests": request.run_automation_tests,
                "status": request.status,
                "idempotency_key": request.idempotency_key,
                "request_hash": request.request_hash,
            },
            [
                {
                    "request_id": request.id,
                    "module_id": item.module_id,
                    "version": item.version,
                    "deployment_order": item.deployment_order,
                }
                for item in request.modules
            ],
        )
        self._requests[request_id] = request
        if idempotency_key:
            self._request_idempotency[idempotency_key] = (request_hash, request_id)
        return next(item for item in self.production_requests() if item["id"] == request_id)

    def approve_request(self, request_id: str, actor: str, comment: str | None = None) -> dict[str, object]:
        request = self._requests.get(request_id)
        if request is None:
            raise KeyError("production request not found")
        if request.status != "waiting_approval":
            raise ValueError("production request is not waiting for approval")
        if len(request.modules) != 1:
            raise PortalError(
                "MULTI_MODULE_ORCHESTRATION_UNAVAILABLE",
                "production requests currently require exactly one module",
                422,
            )
        requested = request.modules[0]
        version = self._version_records.get((requested.module_id, requested.version)) or {}
        pipeline_run_id = version.get("pipelineRunId")
        artifact_digest = version.get("artifactDigest")
        if not pipeline_run_id or not artifact_digest:
            raise PortalError(
                "VERSION_NOT_PROMOTABLE",
                "registered version is not linked to a verified pipeline artifact",
                409,
            )
        report = version.get("ciReport")
        if request.run_automation_tests and (
            not isinstance(report, dict) or report.get("autoTest") != "passed"
        ):
            raise PortalError(
                "AUTOMATION_GATE_FAILED",
                "production request requires a passing automation-test result on the selected version",
                409,
            )
        deployment = self.platform.create_production_promotion(
            UUID(str(pipeline_run_id)),
            requested_by=request.requested_by,
            correlation_id=f"production-request:{request.id}",
            production_request_id=request.id,
            scheduled_for=request.scheduled_for,
            rollback_strategy=request.rollback_strategy,
            run_automation_tests=request.run_automation_tests,
            deployment_parameters=self.delivery_parameters(requested.module_id, Environment.PROD),
        )
        next_comment = comment or f"approved by {actor}"
        self._persist("update_request", request_id, "approved", next_comment, deployment.id)
        request.status = "approved"
        request.comment = next_comment
        request.deployment_id = deployment.id
        try:
            self.platform.approve_deployment(deployment.id, actor)
        except Exception as exc:
            blocked_comment = f"deployment could not start: {exc}"
            self._persist("update_request", request_id, "blocked", blocked_comment, deployment.id)
            request.status = "blocked"
            request.comment = blocked_comment
            raise
        return next(item for item in self.production_requests() if item["id"] == request_id)

    def reject_request(self, request_id: str, actor: str, comment: str | None = None) -> dict[str, object]:
        return self._set_request_status(request_id, "rejected", actor, comment)

    def record_production_deployment_result(
        self,
        deployment_id: UUID,
        status: str,
        message: str | None,
    ) -> None:
        request = next(
            (item for item in self._requests.values() if item.deployment_id == deployment_id),
            None,
        )
        if request is None:
            return
        target = "succeeded" if status == "healthy" else "blocked"
        if request.status == target:
            return
        comment = message or f"deployment {status}"
        self._persist("update_request", request.id, target, comment, deployment_id)
        request.status = target
        request.comment = comment

    def _set_request_status(self, request_id: str, status: str, actor: str, comment: str | None) -> dict[str, object]:
        request = self._requests.get(request_id)
        if request is None:
            raise KeyError("production request not found")
        if request.status != "waiting_approval":
            raise ValueError("production request is not waiting for approval")
        next_comment = comment or f"{status} by {actor}"
        self._persist("update_request", request_id, status, next_comment, None)
        request.status = status
        request.comment = next_comment
        return next(item for item in self.production_requests() if item["id"] == request_id)

    def servers(self, application_ids: set[UUID] | None = None) -> list[dict[str, object]]:
        result: list[dict[str, object]] = []
        seen: set[tuple[str, str, str]] = set()
        for module in self._modules.values():
            if application_ids is not None and module.application_id not in application_ids:
                continue
            for target in module.deployment_environments:
                environment = str(target.get("environment") or "dev")
                for hostname in target.get("servers") or []:
                    key = (module.system_id, environment, str(hostname))
                    if key in seen:
                        continue
                    seen.add(key)
                    result.append(
                        {
                            "id": f"{module.id}:{environment}:{hostname}",
                            "hostname": str(hostname),
                            "systemId": module.system_id,
                            "moduleId": module.id,
                            "ipAddress": "",
                            "environment": environment,
                            "status": "unknown",
                            "kind": "configured-runtime-target",
                            "runtime": module.runtime.value,
                        }
                    )
        return result

    def dcim_services(self, query: str) -> dict[str, object]:
        page = self.dcim_catalog.search_services(query)
        return {"source": page.source, "status": page.status, "items": page.items}

    def dcim_modules(self, system_id: str) -> dict[str, object]:
        if system_id not in self._systems:
            raise KeyError("system not found")
        page = self.dcim_catalog.list_modules(system_id)
        registered_ids = set(self._systems[system_id].module_ids)
        items = [
            {**item, "registered": str(item.get("id")) in registered_ids}
            for item in page.items
        ]
        return {"source": page.source, "status": page.status, "systemId": system_id, "items": items}

    def dcim_servers(self, system_id: str, module_id: str | None = None) -> dict[str, object]:
        if system_id not in self._systems:
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
        if module_id is not None and module_id not in self._modules:
            raise KeyError("module not found")
        if system_id is not None and system_id not in self._systems:
            raise KeyError("system not found")

        application_to_module = {
            module.application_id: module for module in self._modules.values() if module.application_id is not None
        }
        allowed_modules: set[str] | None = None
        if module_id is not None:
            allowed_modules = {module_id}
        elif system_id is not None:
            allowed_modules = set(self._systems[system_id].module_ids)

        allowed_application_ids = {
            module.application_id
            for module in self._modules.values()
            if module.application_id is not None
            and (allowed_modules is None or module.id in allowed_modules)
        }
        if application_ids is not None:
            allowed_application_ids &= application_ids
        records = self.platform.audit_records(allowed_application_ids)
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

    def _module_runs_raw(self, module: PortalModule):
        if module.application_id is None:
            return ()
        return self.platform.list_pipeline_runs(module.application_id)

    def _module_runs(self, module: PortalModule) -> int:
        return len(self._module_runs_raw(module))

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
            "startedBy": run.started_by,
        }

    @staticmethod
    def _environment_status(deployments, environment: Environment) -> str:
        for deployment in reversed(deployments):
            if deployment.environment == environment:
                return deployment.status.value.replace("pending_approval", "pending").replace("rolled_back", "rolled back")
        return "not_deployed"

    def dora_projection(self, application_ids: list[UUID]) -> dict[str, object]:
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
            source.extend(self.platform.delivery_events(application_id))
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

    def _module_application_ids(self, module_id: str) -> list[UUID]:
        module = self._modules[module_id]
        return [module.application_id] if module.application_id else []

    def _dora(self, module_id: str) -> list[dict[str, object]]:
        metrics = self.dora_projection(self._module_application_ids(module_id))["metrics"]
        assert isinstance(metrics, list)
        return metrics

    def _activity_by_day(self, application_ids: set[UUID] | None = None) -> list[dict[str, object]]:
        today = datetime.now(timezone.utc).date()
        all_runs = []
        for mod in self._modules.values():
            if mod.application_id and (application_ids is None or mod.application_id in application_ids):
                all_runs.extend(self.platform.list_pipeline_runs(mod.application_id))

        days = []
        for offset in range(6, -1, -1):
            day_date = today - timedelta(days=offset)
            succeeded = sum(1 for r in all_runs if r.created_at.date() == day_date and r.status.value == "succeeded")
            failed = sum(1 for r in all_runs if r.created_at.date() == day_date and r.status.value == "failed")
            days.append({"date": day_date.isoformat(), "succeeded": succeeded, "failed": failed})
        return days

    @staticmethod
    def _now(minutes_ago: int) -> datetime:
        return datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
