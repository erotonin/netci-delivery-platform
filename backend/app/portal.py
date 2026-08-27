"""Release Portal read models and portal-facing commands.

This layer adapts the delivery domain (applications, pipeline runs and deployments)
to the System -> Module -> Release hierarchy used by the Custom Portal. It is
intentionally isolated from transport and can later be backed by PostgreSQL
projection queries without changing the HTTP response shapes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from .delivery import DeliveryPlatform
from .domain.models import Environment, Runtime
from .persistence import PostgresPortalStore


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


class PortalReadModel:
    """Small local projection with stable response shapes for the Portal UI."""

    def __init__(self, platform: DeliveryPlatform) -> None:
        self.platform = platform
        self.store = PostgresPortalStore.from_env()
        self._systems: dict[str, PortalSystem] = {}
        self._modules: dict[str, PortalModule] = {}
        self._requests: dict[str, PortalProductionRequest] = {}
        self._request_idempotency: dict[str, tuple[tuple[object, ...], str]] = {}
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
        except Exception as exc:
            raise PortalError(
                "PERSISTENCE_UNAVAILABLE",
                f"cannot persist portal state: {exc}",
                503,
            ) from exc

    def _seed(self) -> None:
        for item in (
            PortalSystem(
                "netChat",
                "Trung tâm nền tảng Công nghệ và Chuyển đổi số",
                "Real-time messaging platform for internal team communication.",
                "Admin",
                "healthy",
                ["backend-api", "web-client"],
            ),
            PortalSystem(
                "PCTT",
                "Trung tâm Chăm sóc khách hàng",
                "Ticketing & customer-support case tracking module.",
                "Admin",
                "degraded",
                ["pctt-api", "pctt-web"],
            ),
            PortalSystem(
                "NocPro5",
                "Trung tâm Vận hành khai thác mạng",
                "Network operations alarm monitoring & correlation.",
                "Admin",
                "critical",
                ["alert-correlator"],
            ),
        ):
            self._systems[item.id] = item

        self._modules.update(
            {
                "backend-api": PortalModule(
                    "backend-api", "netChat", "Backend API", "Backend",
                    "Node.js REST & WebSocket API for auth, messaging, presence.",
                    Runtime.DOCKER, versions=["v2.4.1", "v2.4.0", "v2.3.8"],
                ),
                "web-client": PortalModule(
                    "web-client", "netChat", "Web Client", "Frontend",
                    "React SPA for desktop & mobile web messaging.",
                    Runtime.KUBERNETES, versions=["v1.9.2", "v1.9.1"],
                ),
                "pctt-api": PortalModule(
                    "pctt-api", "PCTT", "PCTT API", "Backend",
                    "Customer support API and ticketing workflow.", Runtime.DOCKER,
                ),
                "pctt-web": PortalModule(
                    "pctt-web", "PCTT", "PCTT Web", "Frontend",
                    "Customer support operations web application.", Runtime.KUBERNETES,
                ),
                "alert-correlator": PortalModule(
                    "alert-correlator", "NocPro5", "Alert Correlator", "Backend",
                    "Network alarm correlation and notification service.", Runtime.SYSTEMD,
                ),
            }
        )
        templates = {
            Runtime.DOCKER: "container-ci-cd-v1",
            Runtime.KUBERNETES: "kubernetes-ci-cd-v1",
            Runtime.SYSTEMD: "systemd-ansible-ci-cd-v1",
        }
        for module in self._modules.values():
            application = self.platform.create_application(
                name=module.id,
                repository_url=f"https://git.example.net/{module.system_id.lower()}/{module.id}",
                pipeline_template=templates[module.runtime],
                runtime=module.runtime,
                default_environment=Environment.DEV,
                stages=[],
                idempotency_key=f"portal-reference-{module.id}",
            )
            module.application_id = application.id
        self._requests.update(
            {
                "pr-backend-241": PortalProductionRequest(
                    "pr-backend-241", [PortalProductionModule("backend-api", "v2.4.1")], "TrungTT",
                    datetime(2025, 4, 30, 3, 0, tzinfo=timezone(timedelta(hours=7))), "automatic", True,
                    "waiting_approval",
                ),
                "pr-web-192": PortalProductionRequest(
                    "pr-web-192", [PortalProductionModule("web-client", "v1.9.2")], "HaiNM",
                    datetime(2025, 4, 29, 2, 0, tzinfo=timezone(timedelta(hours=7))), "automatic", True,
                    "approved",
                ),
                "pr-alert-084": PortalProductionRequest(
                    "pr-alert-084", [PortalProductionModule("alert-correlator", "v0.8.4")], "MinhNV",
                    datetime(2025, 4, 25, 22, 0, tzinfo=timezone(timedelta(hours=7))), "manual", False,
                    "blocked",
                ),
            }
        )
        self._version_records.update(
            {
                ("backend-api", "v2.4.1"): {
                    "gitTagUrl": "https://git.example.net/netchat/backend-api/-/tags/v2.4.1",
                    "artifactUrl": "https://artifacts.example.net/netchat/backend-api/v2.4.1",
                    "createdBy": "TrungTT",
                    "createdAt": "2025-04-28T09:14:00+07:00",
                    "ciReport": {
                        "coverage": 87,
                        "autoTest": "passed",
                        "sast": "passed",
                        "sastIssues": 0,
                        "vulnerabilities": {"critical": 0, "high": 0, "medium": 2},
                        "commit": "a1c4e2f",
                    },
                },
                ("backend-api", "v2.4.0"): {
                    "gitTagUrl": "https://git.example.net/netchat/backend-api/-/tags/v2.4.0",
                    "artifactUrl": "https://artifacts.example.net/netchat/backend-api/v2.4.0",
                    "createdBy": "HaiNM",
                    "createdAt": "2025-04-10T14:22:00+07:00",
                    "ciReport": {
                        "coverage": 84,
                        "autoTest": "passed",
                        "sast": "passed",
                        "sastIssues": 0,
                        "vulnerabilities": {"critical": 0, "high": 1, "medium": 3},
                        "commit": "7bd11ca",
                    },
                },
            }
        )

    def create_system(self, *, system_id: str, unit: str, description: str, owner: str) -> dict[str, object]:
        if system_id in self._systems:
            raise ValueError("system already exists")
        record = PortalSystem(system_id, unit, description, owner, "healthy", [])
        self._persist(
            "insert_system",
            {"id": system_id, "unit": unit, "description": description, "owner": owner, "status": "healthy"},
        )
        self._systems[system_id] = record
        return self.system(system_id)

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

    def systems(self) -> list[dict[str, object]]:
        return [self.system(item.id) for item in self._systems.values()]

    def system(self, system_id: str) -> dict[str, object]:
        item = self._systems.get(system_id)
        if item is None:
            raise KeyError("system not found")
        modules = [self._modules[module_id] for module_id in item.module_ids if module_id in self._modules]
        runs = sum(self._module_runs(module) for module in modules)
        failed = sum(1 for module in modules for run in self._module_runs_raw(module) if run.status.value == "failed")
        return {
            "id": item.id,
            "unit": item.unit,
            "description": item.description,
            "owner": item.owner,
            "status": item.status,
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

    def dashboard(self) -> dict[str, object]:
        systems = self.systems()
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
            "pipelineActivity": self._activity_by_day(),
            "systems": systems,
        }

    def module_overview(self, module_id: str) -> dict[str, object]:
        module = self.module(module_id)
        return {
            "module": module,
            "mergeRequests": [
                {"id": "MR-239", "branch": "main", "status": "running"},
                {"id": "MR-241", "branch": "main", "status": "succeeded"},
                {"id": "MR-240", "branch": "develop", "status": "failed"},
            ],
            "deployments": [
                {"environment": "dev", "status": "deployed"},
                {"environment": "staging", "status": "deployed"},
            ],
            "recentReleases": [
                {"version": version, "status": "deployed", "testStatus": "passed"}
                for version in module["versions"][:3]
            ],
            "trends": {
                "testCoverage": 87,
                "automationPassRate": 100,
                "securityFindings": {"total": 10, "critical": 0, "high": 2, "medium": 4, "low": 4},
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
        environments = [
            {"dev": "deployed", "staging": "deployed", "prod": "deployed"},
            {"dev": "deployed", "staging": "deployed", "prod": "superseded"},
            {"dev": "deployed", "staging": "not_deployed", "prod": "not_deployed"},
        ]
        return {
            "moduleId": module_id,
            "items": [
                {
                    "version": version,
                    "artifactDigest": None,
                    "signed": False,
                    "sbom": "not_available",
                    "scan": "not_available",
                    "environments": environments[index] if index < len(environments) else environments[-1],
                    **self._version_records.get((module_id, version), {}),
                }
                for index, version in enumerate(module.versions)
            ],
        }

    def register_version(
        self,
        module_id: str,
        *,
        tag: str,
        git_tag_url: str,
        artifact_url: str,
        created_by: str = "Admin",
    ) -> dict[str, object]:
        module = self._modules.get(module_id)
        if module is None:
            raise KeyError("module not found")
        if tag in module.versions:
            raise ValueError("version already exists")
        record = {
            "gitTagUrl": git_tag_url,
            "artifactUrl": artifact_url,
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
            return {"scope": "module", "scopeId": scope_id, "metrics": self._dora(scope_id)}
        if scope_id in self._systems:
            module_ids = self._systems[scope_id].module_ids
            metric_sets = [self._dora(module_id) for module_id in module_ids]
            return {"scope": "system", "scopeId": scope_id, "metrics": self._merge_metrics(metric_sets)}
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
        signature: tuple[object, ...] = (
            tuple((str(item["moduleId"]), str(item["version"]), int(item["deploymentOrder"])) for item in modules),
            requested_by,
            scheduled_for.isoformat(),
            rollback_strategy,
            run_automation_tests,
        )
        if idempotency_key and idempotency_key in self._request_idempotency:
            existing_signature, existing_id = self._request_idempotency[idempotency_key]
            if existing_signature != signature:
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
            self._request_idempotency[idempotency_key] = (signature, request_id)
        return next(item for item in self.production_requests() if item["id"] == request_id)

    def approve_request(self, request_id: str, actor: str, comment: str | None = None) -> dict[str, object]:
        return self._set_request_status(request_id, "approved", actor, comment)

    def reject_request(self, request_id: str, actor: str, comment: str | None = None) -> dict[str, object]:
        return self._set_request_status(request_id, "rejected", actor, comment)

    def _set_request_status(self, request_id: str, status: str, actor: str, comment: str | None) -> dict[str, object]:
        request = self._requests.get(request_id)
        if request is None:
            raise KeyError("production request not found")
        if request.status != "waiting_approval":
            raise ValueError("production request is not waiting for approval")
        next_comment = comment or f"{status} by {actor}"
        self._persist("update_request", request_id, status, next_comment)
        request.status = status
        request.comment = next_comment
        return next(item for item in self.production_requests() if item["id"] == request_id)

    def servers(self) -> list[dict[str, object]]:
        return [
            {"id": "srv-dev-01", "hostname": "srv-dev-01", "systemId": "netChat", "ipAddress": "10.60.12.21", "environment": "dev", "status": "online", "kind": "runtime-target", "runtime": "docker"},
            {"id": "srv-dev-02", "hostname": "srv-dev-02", "systemId": "netChat", "ipAddress": "10.60.12.22", "environment": "dev", "status": "online", "kind": "runtime-target", "runtime": "docker"},
            {"id": "srv-stg-01", "hostname": "srv-stg-01", "systemId": "netChat", "ipAddress": "10.60.18.31", "environment": "staging", "status": "maintenance", "kind": "runtime-target", "runtime": "docker"},
            {"id": "srv-prod-01", "hostname": "srv-prod-01", "systemId": "netChat", "ipAddress": "10.60.24.41", "environment": "prod", "status": "online", "kind": "runtime-target", "runtime": "docker"},
            {"id": "srv-prod-02", "hostname": "srv-prod-02", "systemId": "netChat", "ipAddress": "10.60.24.42", "environment": "prod", "status": "online", "kind": "runtime-target", "runtime": "docker"},
            {"id": "pctt-app-01", "hostname": "pctt-app-01", "systemId": "PCTT", "ipAddress": "10.61.20.11", "environment": "prod", "status": "online", "kind": "runtime-target", "runtime": "kubernetes"},
            {"id": "nocpro5-01", "hostname": "nocpro5-01", "systemId": "NocPro5", "ipAddress": "10.62.10.15", "environment": "prod", "status": "offline", "kind": "runtime-target", "runtime": "systemd"},
            {"id": "nocpro5-02", "hostname": "nocpro5-02", "systemId": "NocPro5", "ipAddress": "10.62.10.16", "environment": "staging", "status": "online", "kind": "runtime-target", "runtime": "systemd"},
            {"id": "jenkins-a", "hostname": "jenkins-a", "systemId": "netCI", "ipAddress": "10.60.2.10", "environment": "prod", "status": "online", "kind": "jenkins-controller", "executors": {"busy": 1, "total": 4}},
            {"id": "jenkins-b", "hostname": "jenkins-b", "systemId": "netCI", "ipAddress": "10.60.2.11", "environment": "prod", "status": "online", "kind": "jenkins-controller", "executors": {"busy": 0, "total": 4}},
            {"id": "kind-local", "hostname": "kind-local", "systemId": "netCI", "ipAddress": "127.0.0.1", "environment": "dev", "status": "online", "kind": "runtime-target", "runtime": "kubernetes"},
        ]

    def dcim_services(self, query: str) -> list[dict[str, object]]:
        services = [
            {"id": "svc-netchat", "name": "netChat", "code": "VTN_CNTT_MSS_686", "tenant": "Trung tâm nền tảng Công nghệ và Chuyển đổi số", "tier": "Tier 2", "description": "Real-time messaging platform for internal team communication."},
            {"id": "svc-pctt", "name": "PCTT", "code": "VTN_CS_PCTT_210", "tenant": "Trung tâm Chăm sóc khách hàng", "tier": "Tier 2", "description": "Ticketing and customer support case tracking."},
            {"id": "svc-nocpro5", "name": "NocPro5", "code": "VTN_NOC_PRO5_005", "tenant": "Trung tâm Vận hành khai thác mạng", "tier": "Tier 1", "description": "Network operations alarm monitoring and correlation."},
            {"id": "svc-eoffice", "name": "eOffice", "code": "VTN_CNTT_EOFFICE_118", "tenant": "Trung tâm nền tảng Công nghệ và Chuyển đổi số", "tier": "Tier 3", "description": "Enterprise document workflow and digital office platform."},
        ]
        normalized = query.strip().lower()
        if not normalized:
            return []
        return [item for item in services if normalized in str(item["name"]).lower() or normalized in str(item["code"]).lower()]

    def dcim_modules(self, system_id: str) -> list[dict[str, object]]:
        if system_id not in self._systems:
            raise KeyError("system not found")
        fixtures = {
            "netChat": [
                {"id": "backend-api", "name": "Backend API", "code": "NETCHAT_BE", "type": "Backend", "repositoryUrl": "https://git.example.net/netchat/backend-api", "registered": True},
                {"id": "web-client", "name": "Web Client", "code": "NETCHAT_WEB", "type": "Frontend", "repositoryUrl": "https://git.example.net/netchat/web-client", "registered": True},
                {"id": "notification-worker", "name": "Notification Worker", "code": "NETCHAT_NOTIFY", "type": "Worker", "repositoryUrl": "https://git.example.net/netchat/notification-worker", "registered": False},
                {"id": "media-service", "name": "Media Service", "code": "NETCHAT_MEDIA", "type": "Backend", "repositoryUrl": "https://git.example.net/netchat/media-service", "registered": False},
                {"id": "edge-gateway", "name": "Edge Gateway", "code": "NETCHAT_EDGE", "type": "Gateway", "repositoryUrl": "https://git.example.net/netchat/edge-gateway", "registered": False},
            ]
        }
        return fixtures.get(system_id, [])

    def audit_events(self, *, system_id: str | None = None, module_id: str | None = None) -> list[dict[str, object]]:
        events = [
            {"id": "audit-1", "action": "pipeline.started", "actor": "TrungTT", "target": "backend-api", "createdAt": self._now(-2).isoformat()},
            {"id": "audit-2", "action": "deployment.approved", "actor": "Admin", "target": "backend-api", "createdAt": self._now(-1).isoformat()},
            {"id": "audit-3", "action": "security.scan.blocked", "actor": "netCI", "target": "alert-correlator", "createdAt": self._now(-3).isoformat()},
        ]
        if system_id:
            module_ids = set(self._systems.get(system_id, PortalSystem("", "", "", "", "", [])).module_ids)
            events = [event for event in events if event["target"] in module_ids or event["target"] == system_id]
        if module_id:
            events = [event for event in events if event["target"] == module_id]
        return events

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
            "status": run.status.value,
            "commitSha": run.commit_sha,
            "branch": run.branch,
            "environment": run.environment.value,
            "parameters": dict(run.parameters),
            "createdAt": run.created_at.isoformat(),
            "updatedAt": run.updated_at.isoformat(),
            "jenkinsRunId": run.jenkins_run_id,
            "workflowId": run.workflow_id,
            "artifactDigest": run.artifact_digest,
        }

    @staticmethod
    def _environment_status(deployments, environment: Environment) -> str:
        for deployment in reversed(deployments):
            if deployment.environment == environment:
                return deployment.status.value.replace("pending_approval", "pending").replace("rolled_back", "rolled back")
        return "not_deployed"

    def _dora(self, module_id: str) -> list[dict[str, object]]:
        module = self._modules[module_id]
        runs = self._module_runs_raw(module)
        deployments = self.platform.list_deployments(module.application_id) if module.application_id else ()
        production = [item for item in deployments if item.environment == Environment.PROD]
        failed = sum(1 for item in runs if item.status.value == "failed")
        frequency = len(production) / 4 if production else 8.2
        return [
            {"key": "deploymentFrequency", "label": "Deployment Frequency", "value": round(frequency, 1), "unit": "/wk", "hint": "Releases per week"},
            {"key": "leadTime", "label": "Lead Time for Changes", "value": 4.5, "unit": "h", "hint": "Commit to production"},
            {"key": "changeFailureRate", "label": "Change Failure Rate", "value": round(failed / len(runs) * 100, 1) if runs else 3.1, "unit": "%", "hint": "Deploys causing incidents"},
            {"key": "timeToRestoreService", "label": "Time to Restore Service", "value": 1.2, "unit": "h", "hint": "Mean recovery time"},
        ]

    @staticmethod
    def _merge_metrics(metric_sets: list[list[dict[str, object]]]) -> list[dict[str, object]]:
        if not metric_sets:
            return []
        result: list[dict[str, object]] = []
        for index in range(len(metric_sets[0])):
            values = [float(metrics[index]["value"]) for metrics in metric_sets]
            result.append({**metric_sets[0][index], "value": round(sum(values) / len(values), 1)})
        return result

    def _activity_by_day(self) -> list[dict[str, object]]:
        today = datetime.now(timezone.utc).date()
        return [
            {"date": (today - timedelta(days=offset)).isoformat(), "succeeded": [4, 0, 6, 5, 8, 11, 7][offset], "failed": [0, 1, 0, 1, 0, 0, 0][offset]}
            for offset in range(6, -1, -1)
        ]

    @staticmethod
    def _now(minutes_ago: int) -> datetime:
        return datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
