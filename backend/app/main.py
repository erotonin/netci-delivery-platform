from __future__ import annotations

import os
import secrets
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from fastapi import FastAPI, Header, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, HttpUrl, model_validator
from starlette.exceptions import HTTPException as StarletteHTTPException

from .domain.models import (
    Application,
    Deployment,
    Environment,
    PipelineRun,
    PipelineStatus,
    Runtime,
)
from .delivery import CiResult, DeliveryError, DeliveryPlatform
from .portal import PortalReadModel


def configured_cors_origins() -> list[str]:
    """Return validated browser origins from the comma-separated environment setting."""

    raw = os.getenv("NETCI_ALLOWED_ORIGINS", "http://localhost:5173")
    origins: list[str] = []
    for candidate in raw.split(","):
        candidate = candidate.strip()
        if not candidate:
            continue
        try:
            parsed = urlsplit(candidate)
            parsed.port
            hostname = parsed.hostname
        except ValueError as exc:
            raise ValueError(
                "NETCI_ALLOWED_ORIGINS must contain comma-separated valid http(s) origins"
            ) from exc
        if (
            candidate == "*"
            or parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or not hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(
                "NETCI_ALLOWED_ORIGINS must contain comma-separated http(s) origins without paths"
            )
        origin = f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"
        if origin not in origins:
            origins.append(origin)
    if not origins:
        raise ValueError("NETCI_ALLOWED_ORIGINS must contain at least one origin")
    return origins

app = FastAPI(title="netCI Delivery API", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=configured_cors_origins(),
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["Authorization", "Content-Type", "Idempotency-Key", "X-Correlation-Id"],
    expose_headers=["X-Correlation-Id"],
)

platform = DeliveryPlatform()
portal = PortalReadModel(platform)


@app.middleware("http")
async def correlation_id_middleware(request: Request, call_next):
    supplied_correlation_id = request.headers.get("X-Correlation-Id")
    correlation_id = supplied_correlation_id or str(uuid4())
    if len(correlation_id) > 128:
        correlation_id = str(uuid4())
        request.state.correlation_id = correlation_id
        response = error(
            "VALIDATION_ERROR",
            "request validation failed",
            correlation_id,
            422,
        )
        response.headers["X-Correlation-Id"] = correlation_id
        return response
    request.state.correlation_id = correlation_id
    response = await call_next(request)
    response.headers["X-Correlation-Id"] = correlation_id
    return response

def error(
    code: str,
    message: str,
    correlation_id: str | None = None,
    http_status: int = 400,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=http_status,
        content={"code": code, "message": message, "correlationId": correlation_id},
        headers=headers,
    )


def require_pipeline_api_key(authorization: str | None) -> None:
    expected = os.getenv("NETCI_PIPELINE_API_KEY")
    if not expected:
        if os.getenv("NETCI_ENVIRONMENT", "local") != "local":
            raise HTTPException(
                status_code=503,
                detail={"code": "PIPELINE_KEY_NOT_CONFIGURED", "message": "pipeline API key is not configured"},
            )
        expected = "netci-local-pipeline-key"
    supplied = authorization.removeprefix("Bearer ") if authorization and authorization.startswith("Bearer ") else ""
    if not supplied or not secrets.compare_digest(supplied, expected):
        raise HTTPException(
            status_code=401,
            detail={"code": "PIPELINE_UNAUTHORIZED", "message": "valid pipeline API key required"},
            headers={"WWW-Authenticate": "Bearer"},
        )


def application_json(item: Application) -> dict[str, object]:
    return {"id": str(item.id), "name": item.name, "repositoryUrl": item.repository_url, "pipelineTemplate": item.pipeline_template, "runtime": item.runtime.value, "defaultEnvironment": item.default_environment.value, "stages": list(item.stages), "createdAt": item.created_at.isoformat()}


def pipeline_json(item: PipelineRun) -> dict[str, object]:
    return {"id": str(item.id), "applicationId": str(item.application_id), "status": item.status.value, "commitSha": item.commit_sha, "branch": item.branch, "environment": item.environment.value, "correlationId": item.correlation_id, "jenkinsRunId": item.jenkins_run_id, "workflowId": item.workflow_id, "artifactDigest": item.artifact_digest, "createdAt": item.created_at.isoformat(), "updatedAt": item.updated_at.isoformat()}


def deployment_json(item: Deployment) -> dict[str, object]:
    return {"id": str(item.id), "applicationId": str(item.application_id), "pipelineRunId": str(item.pipeline_run_id) if item.pipeline_run_id else None, "runtime": item.runtime.value, "environment": item.environment.value, "status": item.status.value, "artifactDigest": item.artifact_digest, "previousArtifactDigest": item.previous_artifact_digest, "approvedBy": item.approved_by, "createdAt": item.created_at.isoformat(), "updatedAt": item.updated_at.isoformat()}


class ApplicationCreate(BaseModel):
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{2,62}$")
    repositoryUrl: HttpUrl
    pipelineTemplate: str
    runtime: Runtime
    defaultEnvironment: Environment = Environment.DEV
    stages: list[str] = Field(default_factory=list)


class PipelineRunCreate(BaseModel):
    commitSha: str = Field(min_length=7)
    branch: str = "main"
    environment: Environment
    parameters: dict[str, object] = Field(default_factory=dict)


class CiResultRequest(BaseModel):
    status: PipelineStatus
    artifactDigest: str | None = None
    logLines: list[str] = Field(default_factory=list, max_length=1000)


class ApprovalRequest(BaseModel):
    comment: str | None = None
    actor: str = "local-reviewer"


class DeploymentResultRequest(BaseModel):
    status: Literal["healthy", "failed"]
    message: str | None = Field(default=None, max_length=2000)


class RollbackRequest(BaseModel):
    targetArtifactDigest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    reason: str = Field(min_length=3)


class SystemCreate(BaseModel):
    id: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9-]{2,62}$")
    unit: str = Field(min_length=2, max_length=200)
    description: str = Field(min_length=2, max_length=1000)
    owner: str = Field(default="Admin", min_length=2, max_length=120)


class ModuleEnvironmentCreate(BaseModel):
    displayName: str = Field(min_length=1, max_length=120)
    environment: Environment
    runtime: Runtime
    servers: list[str] = Field(default_factory=list, max_length=200)
    tasks: list[str] = Field(default_factory=list, max_length=100)
    kubeconfigRef: str | None = Field(default=None, min_length=1, max_length=255)
    namespace: str | None = Field(default=None, pattern=r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$", max_length=63)

    @model_validator(mode="after")
    def validate_target_connection(self) -> "ModuleEnvironmentCreate":
        if self.runtime == Runtime.KUBERNETES:
            if not self.kubeconfigRef or not self.namespace:
                raise ValueError("Kubernetes environments require kubeconfigRef and namespace")
            if self.servers:
                raise ValueError("Kubernetes environments must not declare server targets")
        else:
            if not self.servers:
                raise ValueError("Docker and Systemd environments require at least one server")
            if self.kubeconfigRef or self.namespace:
                raise ValueError("Docker and Systemd environments must not declare Kubernetes credentials")
        return self


class ModuleCreate(BaseModel):
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{2,62}$")
    displayName: str | None = Field(default=None, min_length=1, max_length=255)
    repositoryUrl: HttpUrl
    pipelineTemplate: Literal["container-ci-cd-v1", "kubernetes-ci-cd-v1", "systemd-ansible-ci-cd-v1"]
    runtime: Runtime
    moduleType: str = Field(default="Backend", min_length=2, max_length=40)
    description: str = Field(default="", max_length=1000)
    defaultEnvironment: Environment = Environment.DEV
    stages: list[str] = Field(default_factory=list)
    deploymentEnvironments: list[ModuleEnvironmentCreate] = Field(min_length=1, max_length=3)

    @model_validator(mode="after")
    def validate_deployment_environments(self) -> "ModuleCreate":
        environments = [item.environment for item in self.deploymentEnvironments]
        if len(environments) != len(set(environments)):
            raise ValueError("deployment environments must be unique")
        if self.defaultEnvironment not in environments:
            raise ValueError("defaultEnvironment must be present in deploymentEnvironments")
        if any(item.runtime != self.runtime for item in self.deploymentEnvironments):
            raise ValueError("all deployment environments must use the application runtime")
        expected_runtime = {
            "container-ci-cd-v1": Runtime.DOCKER,
            "kubernetes-ci-cd-v1": Runtime.KUBERNETES,
            "systemd-ansible-ci-cd-v1": Runtime.SYSTEMD,
        }.get(self.pipelineTemplate)
        if expected_runtime is not None and expected_runtime != self.runtime:
            raise ValueError("pipelineTemplate must match the application runtime")
        return self


class PortalApprovalRequest(BaseModel):
    actor: str = Field(default="local-reviewer", min_length=2, max_length=120)
    comment: str | None = Field(default=None, max_length=1000)


class VersionCreate(BaseModel):
    tag: str = Field(pattern=r"^v?\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")
    gitTagUrl: HttpUrl
    artifactUrl: HttpUrl
    createdBy: str = Field(default="Admin", min_length=2, max_length=120)


class VulnerabilityCounts(BaseModel):
    critical: int = Field(default=0, ge=0)
    high: int = Field(default=0, ge=0)
    medium: int = Field(default=0, ge=0)


class VersionCiReport(BaseModel):
    coverage: float = Field(ge=0, le=100)
    autoTest: Literal["passed", "failed", "skipped"]
    sast: Literal["passed", "failed"]
    sastIssues: int = Field(ge=0)
    vulnerabilities: VulnerabilityCounts
    commit: str = Field(pattern=r"^[0-9a-fA-F]{7,64}$")


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    correlation_id = request.state.correlation_id
    detail = exc.detail if isinstance(exc.detail, dict) else {"code": "HTTP_ERROR", "message": str(exc.detail)}
    return error(
        detail.get("code", "HTTP_ERROR"),
        detail.get("message", "request failed"),
        correlation_id,
        exc.status_code,
        dict(exc.headers) if exc.headers else None,
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    return error("VALIDATION_ERROR", "request validation failed", request.state.correlation_id, 422)


@app.exception_handler(DeliveryError)
async def delivery_exception_handler(request: Request, exc: DeliveryError) -> JSONResponse:
    return error(exc.code, exc.message, request.state.correlation_id, exc.status_code)


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok", "version": "0.1.0"}


@app.get("/stage-catalog")
def stage_catalog() -> dict[str, list[dict[str, object]]]:
    return platform.stage_catalog()


@app.get("/portal/dashboard")
def portal_dashboard() -> dict[str, object]:
    return portal.dashboard()


@app.get("/dcim/services")
def search_dcim_services(query: str = "") -> dict[str, object]:
    return {"source": "fixture", "items": portal.dcim_services(query)}


@app.get("/dcim/modules")
def list_dcim_modules(systemId: str) -> dict[str, object]:
    try:
        return {"source": "fixture", "systemId": systemId, "items": portal.dcim_modules(systemId)}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail={"code": "SYSTEM_NOT_FOUND", "message": str(exc)}) from exc


@app.get("/systems")
def list_systems() -> list[dict[str, object]]:
    return portal.systems()


@app.post("/systems", status_code=status.HTTP_201_CREATED)
def create_system(payload: SystemCreate) -> dict[str, object]:
    try:
        return portal.create_system(
            system_id=payload.id,
            unit=payload.unit,
            description=payload.description,
            owner=payload.owner,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail={"code": "SYSTEM_EXISTS", "message": str(exc)}) from exc


@app.get("/systems/{systemId}")
def get_system(systemId: str) -> dict[str, object]:
    try:
        return portal.system(systemId)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail={"code": "SYSTEM_NOT_FOUND", "message": str(exc)}) from exc


@app.get("/systems/{systemId}/modules")
def list_system_modules(systemId: str) -> list[dict[str, object]]:
    try:
        return list(portal.system(systemId)["modules"])
    except KeyError as exc:
        raise HTTPException(status_code=404, detail={"code": "SYSTEM_NOT_FOUND", "message": str(exc)}) from exc


@app.post("/systems/{systemId}/modules", status_code=status.HTTP_201_CREATED)
def create_module(
    systemId: str,
    payload: ModuleCreate,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", min_length=1, max_length=128),
) -> dict[str, object]:
    try:
        application = platform.create_application(
            name=payload.name,
            repository_url=str(payload.repositoryUrl),
            pipeline_template=payload.pipelineTemplate,
            runtime=payload.runtime,
            default_environment=payload.defaultEnvironment,
            stages=payload.stages,
            idempotency_key=idempotency_key,
        )
        return portal.attach_module(
            system_id=systemId,
            module_id=payload.name,
            name=payload.displayName or payload.name,
            module_type=payload.moduleType,
            description=payload.description,
            runtime=payload.runtime,
            application_id=application.id,
            deployment_environments=[item.model_dump(mode="json") for item in payload.deploymentEnvironments],
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail={"code": "SYSTEM_NOT_FOUND", "message": str(exc)}) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail={"code": "MODULE_EXISTS", "message": str(exc)}) from exc


@app.get("/modules/{moduleId}")
def get_module(moduleId: str) -> dict[str, object]:
    try:
        return portal.module(moduleId)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail={"code": "MODULE_NOT_FOUND", "message": str(exc)}) from exc


@app.get("/modules/{moduleId}/overview")
def get_module_overview(moduleId: str) -> dict[str, object]:
    try:
        return portal.module_overview(moduleId)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail={"code": "MODULE_NOT_FOUND", "message": str(exc)}) from exc


@app.post("/modules/{moduleId}/pipeline-runs", status_code=status.HTTP_202_ACCEPTED)
def start_module_pipeline_run(
    moduleId: str,
    payload: PipelineRunCreate,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", min_length=1, max_length=128),
) -> dict[str, object]:
    try:
        module = portal.module(moduleId)
        application_id = module.get("applicationId")
        if not application_id:
            raise DeliveryError("MODULE_NOT_PROVISIONED", "module has no delivery application", 409)
        run = platform.start_pipeline(
            UUID(str(application_id)),
            commit_sha=payload.commitSha,
            branch=payload.branch,
            environment=payload.environment,
            parameters=payload.parameters,
            correlation_id=request.state.correlation_id,
            idempotency_key=idempotency_key,
        )
        return pipeline_json(run)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail={"code": "MODULE_NOT_FOUND", "message": str(exc)}) from exc


@app.get("/modules/{moduleId}/pipeline-runs")
def list_module_pipeline_runs(moduleId: str) -> dict[str, object]:
    try:
        return portal.pipeline_runs(moduleId)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail={"code": "MODULE_NOT_FOUND", "message": str(exc)}) from exc


@app.get("/modules/{moduleId}/versions")
def list_module_versions(moduleId: str) -> dict[str, object]:
    try:
        return portal.versions(moduleId)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail={"code": "MODULE_NOT_FOUND", "message": str(exc)}) from exc


@app.post("/modules/{moduleId}/versions", status_code=status.HTTP_201_CREATED)
def create_module_version(moduleId: str, payload: VersionCreate) -> dict[str, object]:
    try:
        return portal.register_version(
            moduleId,
            tag=payload.tag,
            git_tag_url=str(payload.gitTagUrl),
            artifact_url=str(payload.artifactUrl),
            created_by=payload.createdBy,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail={"code": "MODULE_NOT_FOUND", "message": str(exc)}) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail={"code": "VERSION_EXISTS", "message": str(exc)}) from exc


@app.post("/modules/{moduleId}/versions/{tag}/ci-report", status_code=status.HTTP_202_ACCEPTED)
def publish_module_ci_report(
    moduleId: str,
    tag: str,
    payload: VersionCiReport,
    authorization: str | None = Header(default=None, alias="Authorization"),
) -> dict[str, object]:
    require_pipeline_api_key(authorization)
    try:
        return portal.record_ci_report(moduleId, tag, payload.model_dump())
    except KeyError as exc:
        raise HTTPException(status_code=404, detail={"code": "MODULE_NOT_FOUND", "message": str(exc)}) from exc


@app.get("/modules/{moduleId}/dora")
def get_module_dora(moduleId: str) -> dict[str, object]:
    try:
        return portal.dora(moduleId)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail={"code": "MODULE_NOT_FOUND", "message": str(exc)}) from exc


@app.get("/systems/{systemId}/dora")
def get_system_dora(systemId: str) -> dict[str, object]:
    try:
        return portal.dora(systemId)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail={"code": "SYSTEM_NOT_FOUND", "message": str(exc)}) from exc


@app.get("/production-requests")
def list_production_requests() -> list[dict[str, object]]:
    return portal.production_requests()


@app.post("/production-requests/{requestId}/approve", status_code=status.HTTP_202_ACCEPTED)
def approve_production_request(requestId: str, payload: PortalApprovalRequest) -> dict[str, object]:
    try:
        return portal.approve_request(requestId, payload.actor, payload.comment)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail={"code": "REQUEST_NOT_FOUND", "message": str(exc)}) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail={"code": "REQUEST_STATE_INVALID", "message": str(exc)}) from exc


@app.post("/production-requests/{requestId}/reject", status_code=status.HTTP_202_ACCEPTED)
def reject_production_request(requestId: str, payload: PortalApprovalRequest) -> dict[str, object]:
    try:
        return portal.reject_request(requestId, payload.actor, payload.comment)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail={"code": "REQUEST_NOT_FOUND", "message": str(exc)}) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail={"code": "REQUEST_STATE_INVALID", "message": str(exc)}) from exc


@app.get("/servers")
def list_servers() -> list[dict[str, object]]:
    return portal.servers()


@app.get("/audit-events")
def list_audit_events(systemId: str | None = None, moduleId: str | None = None) -> list[dict[str, object]]:
    return portal.audit_events(system_id=systemId, module_id=moduleId)


@app.post("/applications", status_code=status.HTTP_201_CREATED, response_model=None)
def create_application(payload: ApplicationCreate, idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", min_length=1, max_length=128)) -> dict[str, object]:
    application = platform.create_application(
        name=payload.name,
        repository_url=str(payload.repositoryUrl),
        pipeline_template=payload.pipelineTemplate,
        runtime=payload.runtime,
        default_environment=payload.defaultEnvironment,
        stages=payload.stages,
        idempotency_key=idempotency_key,
    )
    return application_json(application)


@app.get("/applications")
def list_applications() -> list[dict[str, object]]:
    return [application_json(item) for item in platform.list_applications()]


@app.post("/applications/{applicationId}/pipeline-runs", status_code=status.HTTP_202_ACCEPTED, response_model=None)
def start_pipeline_run(applicationId: UUID, payload: PipelineRunCreate, request: Request, idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", min_length=1, max_length=128)) -> JSONResponse | dict[str, object]:
    run = platform.start_pipeline(
        applicationId,
        commit_sha=payload.commitSha,
        branch=payload.branch,
        environment=payload.environment,
        parameters=payload.parameters,
        correlation_id=request.state.correlation_id,
        idempotency_key=idempotency_key,
    )
    return pipeline_json(run)


@app.get("/pipeline-runs/{pipelineRunId}")
def get_pipeline_run(pipelineRunId: UUID) -> dict[str, object]:
    return pipeline_json(platform.get_pipeline(pipelineRunId))


@app.get("/pipeline-runs/{pipelineRunId}/logs")
def get_pipeline_logs(pipelineRunId: UUID) -> dict[str, object]:
    run, lines = platform.get_pipeline_logs(pipelineRunId)
    return {"pipelineRunId": str(pipelineRunId), "correlationId": run.correlation_id, "lines": list(lines)}


@app.post("/pipeline-runs/{pipelineRunId}/ci-result", status_code=status.HTTP_202_ACCEPTED)
def record_ci_result(
    pipelineRunId: UUID,
    payload: CiResultRequest,
    authorization: str | None = Header(default=None, alias="Authorization"),
) -> dict[str, object]:
    require_pipeline_api_key(authorization)
    result: CiResult = platform.record_ci_result(
        pipelineRunId,
        payload.status.value,
        payload.artifactDigest,
        payload.logLines,
    )
    return {
        "pipelineRun": pipeline_json(result.pipeline_run),
        "deployment": deployment_json(result.deployment) if result.deployment else None,
    }


@app.post("/deployments/{deploymentId}/approve", status_code=status.HTTP_202_ACCEPTED, response_model=None)
def approve_deployment(deploymentId: UUID, payload: ApprovalRequest) -> JSONResponse | dict[str, object]:
    return deployment_json(platform.approve_deployment(deploymentId, payload.actor))


@app.get("/deployments/{deploymentId}")
def get_deployment(deploymentId: UUID) -> dict[str, object]:
    return deployment_json(platform.get_deployment(deploymentId))


@app.post("/deployments/{deploymentId}/result", status_code=status.HTTP_202_ACCEPTED)
def record_deployment_result(
    deploymentId: UUID,
    payload: DeploymentResultRequest,
    authorization: str | None = Header(default=None, alias="Authorization"),
) -> dict[str, object]:
    require_pipeline_api_key(authorization)
    return deployment_json(
        platform.record_deployment_result(deploymentId, payload.status, payload.message)
    )


@app.post("/deployments/{deploymentId}/rollback", status_code=status.HTTP_202_ACCEPTED, response_model=None)
def rollback_deployment(deploymentId: UUID, payload: RollbackRequest) -> JSONResponse | dict[str, object]:
    return deployment_json(
        platform.rollback_deployment(deploymentId, payload.targetArtifactDigest)
    )
