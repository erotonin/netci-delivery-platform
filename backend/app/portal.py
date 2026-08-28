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
from .domain.models import DeliveryEvent, Environment, Runtime
from .persistence import PostgresPortalStore
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
            self._modules[app_id] = PortalModule(
                id=app_id,
                system_id=app_id,
                name=app_name,
                module_type="Backend" if runtime != Runtime.KUBERNETES else "Workload",
                description=desc,
                runtime=runtime,
                application_id=app.id,
                deployment_environments=[
                    {
                        "displayName": "Development",
                        "environment": "dev",
                        "runtime": runtime.value,
                        "servers": ["localhost"],
                        "tasks": ["Health check"],
                    }
                ],
                pipeline_config={"runner": "local", "strategy": "Trunk-based"},
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
        result: list[dict[str, object]] = [
            {"id": "localhost", "hostname": "localhost", "systemId": "hello-container", "ipAddress": "127.0.0.1", "environment": "dev", "status": "online", "kind": "runtime-target", "runtime": "docker"},
            {"id": "kind-local", "hostname": "kind-local", "systemId": "hello-kubernetes", "ipAddress": "127.0.0.1", "environment": "dev", "status": "online", "kind": "runtime-target", "runtime": "kubernetes"},
            {"id": "jenkins-local", "hostname": "jenkins-local", "systemId": "netCI", "ipAddress": "127.0.0.1", "environment": "dev", "status": "online", "kind": "jenkins-controller", "executors": {"busy": 0, "total": 4}},
        ]
        for sys_id in self._systems:
            if sys_id not in [str(r["systemId"]) for r in result]:
                result.append({"id": f"srv-{sys_id}", "hostname": f"srv-{sys_id}", "systemId": sys_id, "ipAddress": "127.0.0.1", "environment": "dev", "status": "online", "kind": "runtime-target", "runtime": "docker"})
        return result

    def dcim_services(self, query: str) -> list[dict[str, object]]:
        normalized = query.strip().lower()
        if not normalized:
            return []
        services = [
            {"id": f"svc-{s.id}", "name": s.id, "code": f"VTN_{s.id.upper()}", "tenant": s.unit, "tier": "Tier 1", "description": s.description}
            for s in self._systems.values()
        ]
        return [item for item in services if normalized in str(item["name"]).lower() or normalized in str(item["code"]).lower()]

    def dcim_modules(self, system_id: str) -> list[dict[str, object]]:
        if system_id not in self._systems:
            raise KeyError("system not found")
        sys_obj = self._systems[system_id]
        modules = [self._modules[m_id] for m_id in sys_obj.module_ids if m_id in self._modules]
        return [
            {
                "id": m.id,
                "name": m.name,
                "code": f"{m.id.upper()}_APP",
                "type": m.module_type,
                "repositoryUrl": f"https://github.com/example/{m.id}",
                "registered": True,
            }
            for m in modules
        ]

    def audit_events(self, *, system_id: str | None = None, module_id: str | None = None) -> list[dict[str, object]]:
        events: list[dict[str, object]] = []
        idx = 1
        for mod in self._modules.values():
            runs = self._module_runs_raw(mod)
            for run in runs:
                events.append({
                    "id": f"audit-{idx}",
                    "action": f"pipeline.{run.status.value}",
                    "actor": str(run.parameters.get("started_by", "operator")),
                    "target": mod.id,
                    "createdAt": run.created_at.isoformat(),
                })
                idx += 1
        if system_id:
            module_ids = set(self._systems.get(system_id, PortalSystem("", "", "", "", "", [])).module_ids)
            events = [e for e in events if e["target"] in module_ids or e["target"] == system_id]
        if module_id:
            events = [e for e in events if e["target"] == module_id]
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

    def _activity_by_day(self) -> list[dict[str, object]]:
        today = datetime.now(timezone.utc).date()
        return [
            {"date": (today - timedelta(days=offset)).isoformat(), "succeeded": [4, 0, 6, 5, 8, 11, 7][offset], "failed": [0, 1, 0, 1, 0, 0, 0][offset]}
            for offset in range(6, -1, -1)
        ]

    @staticmethod
    def _now(minutes_ago: int) -> datetime:
        return datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
