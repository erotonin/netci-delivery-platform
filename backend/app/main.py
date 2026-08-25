from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from uuid import UUID

from fastapi import FastAPI, Header, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, HttpUrl

from .domain.models import (
    Application,
    Deployment,
    DeploymentStatus,
    Environment,
    PipelineRun,
    PipelineStatus,
    Runtime,
)

app = FastAPI(title="netCI Delivery API", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

applications: dict[UUID, Application] = {}
pipeline_runs: dict[UUID, PipelineRun] = {}
deployments: dict[UUID, Deployment] = {}
pipeline_logs: dict[UUID, list[str]] = {}
idempotency_records: dict[str, tuple[str, dict[str, object]]] = {}

TEMPLATES: dict[str, dict[str, object]] = {
    "container-ci-cd-v1": {"runtime": Runtime.DOCKER, "stages": ["checkout", "unit-test", "build", "sbom", "vulnerability-scan", "sign", "publish", "deploy", "health-check"]},
    "kubernetes-ci-cd-v1": {"runtime": Runtime.KUBERNETES, "stages": ["checkout", "unit-test", "build", "sbom", "vulnerability-scan", "sign", "publish", "deploy", "health-check"]},
    "systemd-ansible-ci-cd-v1": {"runtime": Runtime.SYSTEMD, "stages": ["checkout", "unit-test", "build", "publish", "deploy", "health-check"]},
}


def error(code: str, message: str, correlation_id: str | None = None, http_status: int = 400) -> JSONResponse:
    return JSONResponse(status_code=http_status, content={"code": code, "message": message, "correlationId": correlation_id})


def payload_hash(payload: object) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def application_json(item: Application) -> dict[str, object]:
    return {"id": str(item.id), "name": item.name, "repositoryUrl": item.repository_url, "pipelineTemplate": item.pipeline_template, "runtime": item.runtime.value, "defaultEnvironment": item.default_environment.value, "stages": list(item.stages), "createdAt": item.created_at.isoformat()}


def pipeline_json(item: PipelineRun) -> dict[str, object]:
    return {"id": str(item.id), "applicationId": str(item.application_id), "status": item.status.value, "commitSha": item.commit_sha, "jenkinsRunId": item.jenkins_run_id, "workflowId": item.workflow_id, "artifactDigest": item.artifact_digest, "createdAt": item.created_at.isoformat(), "updatedAt": item.updated_at.isoformat()}


def deployment_json(item: Deployment) -> dict[str, object]:
    return {"id": str(item.id), "applicationId": str(item.application_id), "runtime": item.runtime.value, "environment": item.environment.value, "status": item.status.value, "artifactDigest": item.artifact_digest, "approvedBy": item.approved_by, "createdAt": item.created_at.isoformat(), "updatedAt": item.updated_at.isoformat()}


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


class ApprovalRequest(BaseModel):
    comment: str | None = None
    actor: str = "local-reviewer"


class RollbackRequest(BaseModel):
    targetArtifactDigest: str = Field(pattern=r"^sha256:")
    reason: str = Field(min_length=3)


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    correlation_id = request.headers.get("X-Correlation-Id")
    detail = exc.detail if isinstance(exc.detail, dict) else {"code": "HTTP_ERROR", "message": str(exc.detail)}
    return error(detail.get("code", "HTTP_ERROR"), detail.get("message", "request failed"), correlation_id, exc.status_code)


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok", "version": "0.1.0"}


@app.get("/stage-catalog")
def stage_catalog() -> dict[str, list[dict[str, object]]]:
    stages = [
        {"id": "checkout", "name": "Checkout source", "category": "source", "enabledByDefault": True},
        {"id": "unit-test", "name": "Unit tests", "category": "test", "enabledByDefault": True},
        {"id": "build", "name": "Build artifact/image", "category": "build", "enabledByDefault": True},
        {"id": "sbom", "name": "Generate SBOM", "category": "security", "enabledByDefault": True},
        {"id": "vulnerability-scan", "name": "Vulnerability scan", "category": "security", "enabledByDefault": True},
        {"id": "sign", "name": "Sign artifact", "category": "publish", "enabledByDefault": True},
        {"id": "publish", "name": "Publish artifact", "category": "publish", "enabledByDefault": True},
        {"id": "deploy", "name": "Deploy through netCI", "category": "deploy", "enabledByDefault": True},
        {"id": "health-check", "name": "Health check", "category": "verify", "enabledByDefault": True},
    ]
    templates = [{"id": key, "name": key, "runtime": value["runtime"].value, "stageIds": value["stages"]} for key, value in TEMPLATES.items()]
    return {"stages": stages, "templates": templates}


@app.post("/applications", status_code=status.HTTP_201_CREATED, response_model=None)
def create_application(payload: ApplicationCreate, idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"), correlation_id: str | None = Header(default=None, alias="X-Correlation-Id")) -> JSONResponse | dict[str, object]:
    request_hash = payload_hash(payload.model_dump(mode="json"))
    if idempotency_key and idempotency_key in idempotency_records:
        previous_hash, previous_response = idempotency_records[idempotency_key]
        if previous_hash != request_hash:
            return error("IDEMPOTENCY_KEY_REUSED", "same key was used with a different request", correlation_id, 409)
        return JSONResponse(status_code=201, content=previous_response)
    template = TEMPLATES.get(payload.pipelineTemplate)
    if template is None:
        return error("TEMPLATE_NOT_FOUND", "pipeline template does not exist", correlation_id, 422)
    if template["runtime"] != payload.runtime:
        return error("RUNTIME_TEMPLATE_MISMATCH", "runtime does not match pipeline template", correlation_id, 422)
    if any(item.name == payload.name for item in applications.values()):
        return error("APPLICATION_EXISTS", "application name already exists", correlation_id, 409)
    application = Application(name=payload.name, repository_url=str(payload.repositoryUrl), pipeline_template=payload.pipelineTemplate, runtime=payload.runtime, default_environment=payload.defaultEnvironment, stages=tuple(payload.stages or template["stages"]))
    applications[application.id] = application
    response = application_json(application)
    if idempotency_key:
        idempotency_records[idempotency_key] = (request_hash, response)
    return response


@app.get("/applications")
def list_applications() -> list[dict[str, object]]:
    return [application_json(item) for item in applications.values()]


@app.post("/applications/{application_id}/pipeline-runs", status_code=status.HTTP_202_ACCEPTED, response_model=None)
def start_pipeline_run(application_id: UUID, payload: PipelineRunCreate, idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"), correlation_id: str | None = Header(default=None, alias="X-Correlation-Id")) -> JSONResponse | dict[str, object]:
    if application_id not in applications:
        return error("APPLICATION_NOT_FOUND", "application not found", correlation_id, 404)
    request_hash = payload_hash({"applicationId": str(application_id), **payload.model_dump(mode="json")})
    if idempotency_key and idempotency_key in idempotency_records:
        previous_hash, previous_response = idempotency_records[idempotency_key]
        if previous_hash != request_hash:
            return error("IDEMPOTENCY_KEY_REUSED", "same key was used with a different request", correlation_id, 409)
        return JSONResponse(status_code=202, content=previous_response)
    run = PipelineRun(application_id=application_id, commit_sha=payload.commitSha, environment=payload.environment)
    pipeline_runs[run.id] = run
    pipeline_logs[run.id] = [f"queued correlationId={correlation_id or 'none'}", f"commit={run.commit_sha}"]
    response = pipeline_json(run)
    if idempotency_key:
        idempotency_records[idempotency_key] = (request_hash, response)
    return response


@app.get("/pipeline-runs/{pipeline_run_id}")
def get_pipeline_run(pipeline_run_id: UUID) -> dict[str, object]:
    run = pipeline_runs.get(pipeline_run_id)
    if run is None:
        raise HTTPException(status_code=404, detail={"code": "PIPELINE_NOT_FOUND", "message": "pipeline run not found"})
    return pipeline_json(run)


@app.get("/pipeline-runs/{pipeline_run_id}/logs")
def get_pipeline_logs(pipeline_run_id: UUID) -> dict[str, object]:
    if pipeline_run_id not in pipeline_runs:
        raise HTTPException(status_code=404, detail={"code": "PIPELINE_NOT_FOUND", "message": "pipeline run not found"})
    return {"pipelineRunId": str(pipeline_run_id), "lines": pipeline_logs.get(pipeline_run_id, [])}


@app.post("/deployments/{deployment_id}/approve", status_code=status.HTTP_202_ACCEPTED, response_model=None)
def approve_deployment(deployment_id: UUID, payload: ApprovalRequest) -> JSONResponse | dict[str, object]:
    deployment = deployments.get(deployment_id)
    if deployment is None:
        raise HTTPException(status_code=404, detail={"code": "DEPLOYMENT_NOT_FOUND", "message": "deployment not found"})
    if deployment.status != DeploymentStatus.PENDING_APPROVAL:
        return error("INVALID_DEPLOYMENT_STATE", "deployment is not waiting for approval", None, 409)
    deployment = Deployment(**{**deployment.__dict__, "status": DeploymentStatus.DEPLOYING, "approved_by": payload.actor, "updated_at": datetime.now(timezone.utc)})
    deployments[deployment.id] = deployment
    return deployment_json(deployment)


@app.post("/deployments/{deployment_id}/rollback", status_code=status.HTTP_202_ACCEPTED, response_model=None)
def rollback_deployment(deployment_id: UUID, payload: RollbackRequest) -> JSONResponse | dict[str, object]:
    deployment = deployments.get(deployment_id)
    if deployment is None:
        raise HTTPException(status_code=404, detail={"code": "DEPLOYMENT_NOT_FOUND", "message": "deployment not found"})
    deployment = Deployment(**{**deployment.__dict__, "status": DeploymentStatus.ROLLED_BACK, "artifact_digest": payload.targetArtifactDigest, "updated_at": datetime.now(timezone.utc)})
    deployments[deployment.id] = deployment
    return deployment_json(deployment)
