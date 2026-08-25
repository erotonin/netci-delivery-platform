from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from fastapi import FastAPI, Header, HTTPException, status
from pydantic import BaseModel, Field, HttpUrl

from .domain.models import (
    Application,
    Environment,
    PipelineRun,
    PipelineStatus,
    Runtime,
)

app = FastAPI(title="netCI Delivery API", version="0.1.0")
applications: dict[UUID, Application] = {}
pipeline_runs: dict[UUID, PipelineRun] = {}


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


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok", "version": "0.1.0"}


@app.get("/stage-catalog")
def stage_catalog() -> dict[str, list[dict[str, object]]]:
    return {
        "stages": [
            {"id": "checkout", "name": "Checkout source", "category": "source", "enabledByDefault": True},
            {"id": "unit-test", "name": "Unit tests", "category": "test", "enabledByDefault": True},
            {"id": "build", "name": "Build artifact/image", "category": "build", "enabledByDefault": True},
            {"id": "sbom", "name": "Generate SBOM", "category": "security", "enabledByDefault": True},
            {"id": "vulnerability-scan", "name": "Vulnerability scan", "category": "security", "enabledByDefault": True},
            {"id": "sign", "name": "Sign artifact", "category": "publish", "enabledByDefault": True},
            {"id": "publish", "name": "Publish artifact", "category": "publish", "enabledByDefault": True},
            {"id": "deploy", "name": "Deploy through netCI", "category": "deploy", "enabledByDefault": True},
            {"id": "health-check", "name": "Health check", "category": "verify", "enabledByDefault": True},
        ],
        "templates": [
            {"id": "container-ci-cd-v1", "name": "Container CI/CD", "runtime": "docker", "stageIds": ["checkout", "unit-test", "build", "sbom", "vulnerability-scan", "sign", "publish", "deploy", "health-check"]},
            {"id": "kubernetes-ci-cd-v1", "name": "Kubernetes CI/CD", "runtime": "kubernetes", "stageIds": ["checkout", "unit-test", "build", "sbom", "vulnerability-scan", "sign", "publish", "deploy", "health-check"]},
            {"id": "systemd-ansible-ci-cd-v1", "name": "Systemd Ansible CI/CD", "runtime": "systemd", "stageIds": ["checkout", "unit-test", "build", "publish", "deploy", "health-check"]},
        ],
    }


@app.post("/applications", status_code=status.HTTP_201_CREATED)
def create_application(payload: ApplicationCreate, idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")) -> dict[str, object]:
    if any(item.name == payload.name for item in applications.values()):
        raise HTTPException(status_code=409, detail="application name already exists")
    application = Application(
        name=payload.name,
        repository_url=str(payload.repositoryUrl),
        pipeline_template=payload.pipelineTemplate,
        runtime=payload.runtime,
        default_environment=payload.defaultEnvironment,
        stages=tuple(payload.stages),
    )
    applications[application.id] = application
    return {
        "id": str(application.id),
        "name": application.name,
        "repositoryUrl": application.repository_url,
        "pipelineTemplate": application.pipeline_template,
        "runtime": application.runtime.value,
        "defaultEnvironment": application.default_environment.value,
        "stages": list(application.stages),
        "createdAt": application.created_at.isoformat(),
    }


@app.get("/applications")
def list_applications() -> list[dict[str, object]]:
    return [
        {
            "id": str(item.id),
            "name": item.name,
            "repositoryUrl": item.repository_url,
            "pipelineTemplate": item.pipeline_template,
            "runtime": item.runtime.value,
            "defaultEnvironment": item.default_environment.value,
            "stages": list(item.stages),
            "createdAt": item.created_at.isoformat(),
        }
        for item in applications.values()
    ]


@app.post("/applications/{application_id}/pipeline-runs", status_code=status.HTTP_202_ACCEPTED)
def start_pipeline_run(application_id: UUID, payload: PipelineRunCreate, correlation_id: str | None = Header(default=None, alias="X-Correlation-Id")) -> dict[str, object]:
    application = applications.get(application_id)
    if application is None:
        raise HTTPException(status_code=404, detail="application not found")
    run = PipelineRun(application_id=application.id, commit_sha=payload.commitSha, environment=payload.environment)
    pipeline_runs[run.id] = run
    return {
        "id": str(run.id),
        "applicationId": str(run.application_id),
        "status": run.status.value,
        "commitSha": run.commit_sha,
        "jenkinsRunId": run.jenkins_run_id,
        "workflowId": run.workflow_id,
        "artifactDigest": run.artifact_digest,
        "createdAt": run.created_at.isoformat(),
        "updatedAt": run.updated_at.isoformat(),
    }


@app.get("/pipeline-runs/{pipeline_run_id}")
def get_pipeline_run(pipeline_run_id: UUID) -> dict[str, object]:
    run = pipeline_runs.get(pipeline_run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="pipeline run not found")
    return {
        "id": str(run.id),
        "applicationId": str(run.application_id),
        "status": run.status.value,
        "commitSha": run.commit_sha,
        "jenkinsRunId": run.jenkins_run_id,
        "workflowId": run.workflow_id,
        "artifactDigest": run.artifact_digest,
        "createdAt": run.created_at.isoformat(),
        "updatedAt": run.updated_at.isoformat(),
    }
