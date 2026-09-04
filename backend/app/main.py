from __future__ import annotations

import hashlib
import os
import secrets
from datetime import datetime, timezone
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator
from starlette.exceptions import HTTPException as StarletteHTTPException

from .adapters.cd_orchestrator import build_cd_orchestrator
from .adapters.ci_launcher import build_ci_launcher
from .adapters.dcim import DcimUnavailable
from .auth import AuthError, Principal, build_authenticator
from .build_inputs import BuildInputError, validate_build_inputs
from .client_address import LOOPBACK_HOSTS, resolve_client
from .ratelimit import build_rate_limiter
from .domain.models import (
    Application,
    DeliveryEvent,
    Deployment,
    Environment,
    PipelineRun,
    PipelineStatus,
    Runtime,
)
from .delivery import CiResult, DeliveryError, DeliveryPlatform
from .policy.rules import (
    PolicyViolation,
    Role,
    require_environment_permission,
    require_separation_of_duties,
    require_team_access,
)
from .demo_data import seed_demo_data
from .portal import PortalError, PortalService
from .store import build_database
from . import workload_identity
from .workload_identity import (
    CallbackClaims,
    Scope,
    Workload,
    WorkloadIdentityError,
)


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

# Re-exported: tests and operators reason about which callers count as local.
__all__ = ["app", "LOOPBACK_HOSTS", "resolve_client"]

app = FastAPI(title="netCI Delivery API", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=configured_cors_origins(),
    allow_credentials=True,
    allow_methods=["GET", "POST", "DELETE", "PUT", "PATCH", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "Idempotency-Key", "X-Correlation-Id"],
    expose_headers=["X-Correlation-Id"],
)

# Composition root: the engines and the store are chosen here from configuration, never
# inside the domain. NETCI_CI_MODE / NETCI_CD_MODE may be "none" only in local mode; outside
# local their factories fail at startup instead of running a control plane that cannot
# execute the work it accepts. `build_database` refuses an in-memory store outside local
# mode for the same reason.
#
# Nothing is read here. Both services answer every request from the database, so a second
# replica is correct the moment it starts rather than serving a snapshot of its own boot.
# Refuses to start outside local mode without a way to tell one workload from another.
# A control plane that accepts "some build says this deployment is healthy" from anyone
# holding a shared secret is not one an operator can reason about.
workload_identity.require_configured_workload_identity()
database = build_database()
platform = DeliveryPlatform(build_ci_launcher(), build_cd_orchestrator(), database=database)
portal = PortalService(platform, database=database)
seed_demo_data(platform, portal)
authenticator = build_authenticator()
rate_limiter = build_rate_limiter()


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

    # Identify the caller by credential where there is one, so a shared NAT does not make
    # a whole office look like a single client and throttle everyone because one person
    # looped. The token is hashed, never stored: the limiter's map would otherwise be a
    # list of live credentials sitting in memory.
    if rate_limiter.enabled and request.url.path != "/healthz":
        authorization = request.headers.get("Authorization", "")
        if authorization:
            caller = "credential:" + hashlib.sha256(authorization.encode()).hexdigest()[:32]
        else:
            # The forwarded client where a proxy chain is configured, so one noisy caller
            # behind the proxy does not throttle everyone sharing the proxy's address.
            caller = "address:" + (resolve_client(request.headers, request.client.host if request.client else "").address or "unknown")
        verdict = rate_limiter.check(caller)
        if not verdict.allowed:
            throttled = error(
                "RATE_LIMITED",
                f"more than {verdict.limit} requests in {int(rate_limiter.window_seconds)}s; "
                "slow down or raise NETCI_RATE_LIMIT",
                correlation_id,
                429,
                {"Retry-After": str(verdict.retry_after)},
            )
            throttled.headers["X-Correlation-Id"] = correlation_id
            throttled.headers["X-RateLimit-Limit"] = str(verdict.limit)
            throttled.headers["X-RateLimit-Remaining"] = "0"
            return throttled

    response = await call_next(request)
    response.headers["X-Correlation-Id"] = correlation_id
    return response

def error(
    code: str,
    message: str,
    correlation_id: str | None = None,
    http_status: int = 400,
    headers: dict[str, str] | None = None,
    detail: dict[str, object] | None = None,
) -> JSONResponse:
    content: dict[str, object] = {"code": code, "message": message, "correlationId": correlation_id}
    if detail is not None:
        content["detail"] = detail
    return JSONResponse(
        status_code=http_status,
        content=content,
        headers=headers,
    )


def _bearer(authorization: str | None) -> str:
    if authorization and authorization.startswith("Bearer "):
        return authorization.removeprefix("Bearer ").strip()
    return ""


def _workload_principal(request: Request, authorization: str | None) -> Principal | None:
    """Resolve a scoped callback token into a machine principal bound to one resource.

    The verified claims are stashed on the request so the endpoint can check that the
    resource in the route is the one the token names. Authentication says "this is a real
    Jenkins token"; only that comparison says "and it is for *this* run".
    """

    supplied = _bearer(authorization)
    if not supplied or supplied.count(".") != 2:
        return None
    try:
        claims = workload_identity.verify(supplied)
    except WorkloadIdentityError as exc:
        raise HTTPException(
            status_code=exc.status,
            detail={"code": exc.code, "message": exc.message},
            headers={"WWW-Authenticate": "Bearer"} if exc.status == 401 else None,
        ) from exc
    request.state.callback_claims = claims
    return Principal(
        subject=f"workload:{claims.workload}",
        display_name=f"netCI {claims.workload} workload",
        email="",
        roles=frozenset({Role.PIPELINE}),
        method="workload-token",
    )


def _pipeline_key_principal(authorization: str | None) -> Principal | None:
    """Recognise the legacy shared pipeline API key.

    One key for every controller and every worker cannot say which build is calling, so
    it is accepted only in local mode or during an explicitly declared migration window
    (`NETCI_ALLOW_LEGACY_PIPELINE_KEY`). Outside those it is refused even when set --
    otherwise "we rolled out workload identity" and "the old key still works" are both
    true, and only the second matters to whoever has the key.
    """

    if not workload_identity.legacy_shared_key_allowed():
        return None
    expected = os.getenv("NETCI_PIPELINE_API_KEY")
    if not expected:
        if os.getenv("NETCI_ENVIRONMENT", "local") != "local":
            raise HTTPException(
                status_code=503,
                detail={"code": "PIPELINE_KEY_NOT_CONFIGURED", "message": "pipeline API key is not configured"},
            )
        expected = "netci-local-pipeline-key"
    supplied = _bearer(authorization)
    if not supplied or not secrets.compare_digest(supplied, expected):
        return None
    return Principal(
        subject="netci-pipeline",
        display_name="netCI pipeline",
        email="",
        roles=frozenset({Role.PIPELINE}),
        method="pipeline-key",
    )


def current_principal(
    request: Request,
    authorization: str | None = Header(default=None, alias="Authorization"),
) -> Principal:
    """Resolve the caller, or refuse the request.

    A scoped workload token is tried first, then the legacy shared key where it is still
    allowed, and only then the configured human authenticator.
    """

    workload = _workload_principal(request, authorization)
    if workload is not None:
        return workload
    machine = _pipeline_key_principal(authorization)
    if machine is not None:
        return machine
    try:
        principal = authenticator.authenticate(authorization)
    except AuthError as exc:
        raise HTTPException(
            status_code=exc.status,
            detail={"code": exc.code, "message": exc.message},
            headers={"WWW-Authenticate": "Bearer"} if exc.status == 401 else None,
        ) from exc

    # An unauthenticated caller is a local developer, and nothing else. Without this,
    # forgetting NETCI_AUTH_MODE on a host with an open port publishes production
    # approval to the network -- a default that fails safe has to fail closed here.
    #
    # `is_trusted_loopback`, not the socket address: behind a reverse proxy on the same
    # host every request arrives from 127.0.0.1, and a naive check would see the whole
    # network as loopback.
    if principal.is_anonymous:
        client = resolve_client(request.headers, request.client.host if request.client else "")
        if not client.is_trusted_loopback:
            detail = (
                "NETCI_AUTH_MODE=none serves loopback only. Set NETCI_AUTH_MODE=token "
                "or oidc to accept requests from the network."
            )
            if client.forwarded and not client.trusted:
                detail += (
                    " This request came through a proxy, so netCI cannot tell where it "
                    "originated; set NETCI_TRUSTED_PROXY_HOPS if netCI is behind one."
                )
            raise HTTPException(
                status_code=403,
                detail={"code": "AUTH_NOT_CONFIGURED", "message": detail},
            )
    return principal


def requires(*roles: Role, unauthenticated_code: str = "UNAUTHENTICATED"):
    """Dependency factory: the caller must hold at least one of these roles.

    `unauthenticated_code` exists so the machine endpoints keep answering with the
    PIPELINE_UNAUTHORIZED code that `scripts/netci_callback.py` and the CI templates
    already recognise; changing it would break every build's error handling for a
    cosmetic gain.
    """

    allowed = frozenset(roles)

    def dependency(principal: Principal = Depends(current_principal)) -> Principal:
        if principal.has_any(*allowed):
            return principal
        required = ", ".join(sorted(role.value for role in allowed))
        # An anonymous caller presented no credential at all, so the answer is "identify
        # yourself" (401), not "you may not" (403). The distinction matters to a client:
        # one is fixed by authenticating, the other by asking for access.
        if principal.is_anonymous:
            raise HTTPException(
                status_code=401,
                detail={
                    "code": unauthenticated_code,
                    "message": f"this action requires one of: {required}",
                },
                headers={"WWW-Authenticate": "Bearer"},
            )
        held = ", ".join(sorted(role.value for role in principal.roles)) or "no roles"
        raise HTTPException(
            status_code=403,
            detail={
                "code": "FORBIDDEN",
                "message": f"this action requires one of: {required}; {principal.subject} holds: {held}",
            },
        )

    return dependency


# Read access is the lowest bar; everything else is named where it is used.
ReadAccess = Depends(requires(Role.VIEWER, Role.DEVELOPER, Role.REVIEWER, Role.PLATFORM_ADMIN, Role.PIPELINE))
DeveloperAccess = Depends(requires(Role.DEVELOPER, Role.PLATFORM_ADMIN))
ReviewerAccess = Depends(requires(Role.REVIEWER, Role.PLATFORM_ADMIN))
AdminAccess = Depends(requires(Role.PLATFORM_ADMIN))
PipelineStartAccess = Depends(requires(Role.DEVELOPER, Role.REVIEWER, Role.PLATFORM_ADMIN))
# The application-scoped run endpoint skips the Portal lookup that binds a run to its
# module's registered deployment target, so it is not a developer-facing route.
LowLevelPipelineAccess = Depends(requires(Role.PLATFORM_ADMIN, Role.PIPELINE))
# Machine-only, with no human escape hatch. These endpoints assert what a build or a
# deployment actually did; a person holding platform-admin has no business forging one,
# and including that role here would also make the pipeline key optional whenever
# NETCI_AUTH_MODE=none, which is exactly when it is most needed.
PipelineAccess = Depends(requires(Role.PIPELINE, unauthenticated_code="PIPELINE_UNAUTHORIZED"))


def _callback_claims(request: Request) -> CallbackClaims | None:
    return getattr(request.state, "callback_claims", None)


def _authorize_callback(
    request: Request,
    *,
    scope: str,
    workload: str | None = None,
    pipeline_run_id: UUID | None = None,
    deployment_id: UUID | None = None,
) -> CallbackClaims | None:
    """Check that the token in hand is for *this* resource and this operation.

    Authentication proved the token is real. This is the part that stops a real token for
    run A from writing to run B, and stops a Jenkins controller from reporting the outcome
    of a deployment it never ran. A legacy shared key has no claims to check, which is
    exactly why it is confined to local and migration modes.
    """

    claims = _callback_claims(request)
    if claims is None:
        return None
    if workload is not None and claims.workload != workload:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "WORKLOAD_NOT_PERMITTED",
                "message": f"this callback may only be made by the {workload} workload",
            },
        )
    if not claims.permits(scope):
        raise HTTPException(
            status_code=403,
            detail={
                "code": "SCOPE_NOT_PERMITTED",
                "message": f"this callback token does not carry the {scope} scope",
            },
        )
    if pipeline_run_id is not None and claims.pipeline_run_id != pipeline_run_id:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "RESOURCE_MISMATCH",
                "message": "this callback token was issued for a different pipeline run",
            },
        )
    if deployment_id is not None and claims.deployment_id != deployment_id:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "RESOURCE_MISMATCH",
                "message": "this callback token was issued for a different deployment",
            },
        )
    if claims.single_use:
        # Claimed in its own transaction and by primary key, so two replicas handed the
        # same replayed token cannot both decide it was unused.
        claimed = _claim_single_use(claims, scope)
        if not claimed:
            raise HTTPException(
                status_code=401,
                detail={
                    "code": "TOKEN_REPLAYED",
                    "message": "this callback token has already been used",
                },
                headers={"WWW-Authenticate": "Bearer"},
            )
    return claims


def _claim_single_use(claims: CallbackClaims, scope: str) -> bool:
    with platform.transaction() as transaction:
        return transaction.claim_callback_token(
            jti=claims.jti,
            workload=claims.workload,
            application_id=claims.application_id,
            operation=scope,
            expires_at=datetime.fromtimestamp(claims.expires_at, tz=timezone.utc),
            pipeline_run_id=claims.pipeline_run_id,
            deployment_id=claims.deployment_id,
        )


def _require_environment_role(environment: Environment, principal: Principal) -> None:
    """Apply the environment policy to the authenticated caller.

    `require_environment_permission` has existed in the policy module since the start but
    was never called, which made production role separation documentation rather than a
    control. This is its call site.
    """

    try:
        require_environment_permission(environment, principal.roles)
    except PolicyViolation as exc:
        raise HTTPException(
            status_code=403, detail={"code": "ENVIRONMENT_FORBIDDEN", "message": str(exc)}
        ) from exc


def require_application_owner() -> bool:
    """Whether an application with no owning team may still be used.

    False during adoption, so applications that predate ownership keep working; true once
    every application has an owner, which makes an unowned one platform-admin only.
    """

    return os.getenv("NETCI_REQUIRE_APPLICATION_OWNER", "false").strip().lower() in {"true", "1", "yes"}


def _require_application_access(application: Application, principal: Principal) -> None:
    """The team half of authorization: which applications this caller may act on."""

    try:
        require_team_access(
            application.owner_team,
            principal.teams,
            is_platform_admin=principal.has_any(Role.PLATFORM_ADMIN),
            require_owner=require_application_owner(),
        )
    except PolicyViolation as exc:
        raise HTTPException(
            status_code=403, detail={"code": "APPLICATION_FORBIDDEN", "message": str(exc)}
        ) from exc


def _can_access_application(application: Application, principal: Principal) -> bool:
    try:
        require_team_access(
            application.owner_team,
            principal.teams,
            is_platform_admin=principal.has_any(Role.PLATFORM_ADMIN),
            require_owner=require_application_owner(),
        )
        return True
    except PolicyViolation:
        return False


def _visible_application_ids(principal: Principal) -> set[UUID]:
    return {
        application.id
        for application in platform.list_applications()
        if _can_access_application(application, principal)
    }


def _application_owner_for_create(owner_team: str | None, principal: Principal) -> str | None:
    """Validate ownership once for both application creation entry points."""

    normalized = (owner_team or "").strip() or None
    if normalized and not principal.has_any(Role.PLATFORM_ADMIN) and not principal.belongs_to(normalized):
        held = ", ".join(sorted(principal.teams)) or "no teams"
        raise HTTPException(
            status_code=403,
            detail={
                "code": "APPLICATION_FORBIDDEN",
                "message": f"cannot create an application owned by {normalized!r}; you belong to {held}",
            },
        )
    if normalized is None and require_application_owner() and not principal.has_any(Role.PLATFORM_ADMIN):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "OWNER_TEAM_REQUIRED",
                "message": "ownerTeam is required: NETCI_REQUIRE_APPLICATION_OWNER is set",
            },
        )
    return normalized


def _require_module_access(module_id: str, principal: Principal) -> dict[str, object]:
    module = portal.module(module_id)
    application_id = module.get("applicationId")
    if application_id:
        _require_application_access(platform.get_application(UUID(str(application_id))), principal)
    return module


def separation_of_duties_enabled(principal: Principal | None = None) -> bool:
    """Whether the requester and the approver must be different people.

    Off for an anonymous caller: with authentication disabled every caller is the same
    subject, so the check would refuse every approval without separating anyone. That
    is a reason to configure authentication, which `/healthz` reports, not a reason to
    pretend the control is active.
    """

    if principal is not None and principal.is_anonymous:
        return False
    return os.getenv("NETCI_REQUIRE_SEPARATION_OF_DUTIES", "true").strip().lower() not in {"false", "0", "no"}


def application_json(item: Application) -> dict[str, object]:
    return {"id": str(item.id), "name": item.name, "repositoryUrl": item.repository_url, "pipelineTemplate": item.pipeline_template, "runtime": item.runtime.value, "defaultEnvironment": item.default_environment.value, "stages": list(item.stages), "ownerTeam": item.owner_team, "createdAt": item.created_at.isoformat()}


def pipeline_json(item: PipelineRun) -> dict[str, object]:
    return {"id": str(item.id), "applicationId": str(item.application_id), "status": item.status.value, "commitSha": item.commit_sha, "branch": item.branch, "environment": item.environment.value, "parameters": dict(item.parameters), "correlationId": item.correlation_id, "jenkinsRunId": item.jenkins_run_id, "workflowId": item.workflow_id, "artifactDigest": item.artifact_digest, "startedBy": item.started_by, "createdAt": item.created_at.isoformat(), "updatedAt": item.updated_at.isoformat()}


def deployment_json(item: Deployment) -> dict[str, object]:
    return {"id": str(item.id), "applicationId": str(item.application_id), "pipelineRunId": str(item.pipeline_run_id) if item.pipeline_run_id else None, "runtime": item.runtime.value, "environment": item.environment.value, "status": item.status.value, "artifactDigest": item.artifact_digest, "previousArtifactDigest": item.previous_artifact_digest, "approvedBy": item.approved_by, "createdAt": item.created_at.isoformat(), "updatedAt": item.updated_at.isoformat()}


def delivery_event_json(item: DeliveryEvent) -> dict[str, object]:
    return {
        "id": str(item.id),
        "eventType": item.event_type.value,
        "applicationId": str(item.application_id),
        "pipelineRunId": str(item.pipeline_run_id) if item.pipeline_run_id else None,
        "deploymentId": str(item.deployment_id) if item.deployment_id else None,
        "commitSha": item.commit_sha,
        "environment": item.environment.value if item.environment else None,
        "successful": item.successful,
        "requiresIntervention": item.requires_intervention,
        "occurredAt": item.occurred_at.isoformat(),
    }


class StrictBody(BaseModel):
    """Request body whose checked-in OpenAPI schema forbids undeclared fields."""

    model_config = ConfigDict(extra="forbid")


class ApplicationCreate(BaseModel):
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{2,62}$")
    # The team accountable for this application. Optional while ownership is being
    # adopted; required once NETCI_REQUIRE_APPLICATION_OWNER is set.
    ownerTeam: str | None = Field(default=None, max_length=255)
    repositoryUrl: HttpUrl
    pipelineTemplate: str
    runtime: Runtime
    defaultEnvironment: Environment = Environment.DEV
    stages: list[str] = Field(default_factory=list)


class PipelineRunCreate(BaseModel):
    # A commit SHA is hexadecimal. Accepting anything else means accepting something that
    # is not a commit, and this value reaches a `git checkout` in the CI template.
    commitSha: str = Field(min_length=7, max_length=64, pattern=r"^[0-9a-fA-F]+$")
    # A branch also reaches a checkout. The character class stops `main; rm -rf /` and
    # `main$(id)`; `validate_reference` additionally rejects `..`, which is both a path
    # escape and an invalid git refname -- a class the character set alone lets through.
    branch: str = Field(default="main", min_length=1, max_length=255, pattern=r"^[0-9a-zA-Z._\-/]+$")
    environment: Environment
    #: Caller-supplied *build* inputs only. Deployment targets, credentials, artifacts and
    #: runtime commands come from the module's registered configuration -- see
    #: `app.build_inputs` for the boundary and why naming one of those keys is a 422.
    parameters: dict[str, object] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_parameters(self) -> "PipelineRunCreate":
        self.parameters = validate_build_inputs(self.parameters)
        return self

    @model_validator(mode="after")
    def validate_reference(self) -> "PipelineRunCreate":
        branch = self.branch
        if ".." in branch or branch.startswith(("/", "-")) or branch.endswith(("/", ".lock")):
            raise ValueError(
                "branch must be a git refname: no '..', no leading '/' or '-', no trailing '/'"
            )
        return self


class CallbackTokenRequest(StrictBody):
    workload: Literal["jenkins", "temporal"]
    scopes: list[str] = Field(min_length=1, max_length=8)
    ttlSeconds: int = Field(default=3600, ge=60, le=86400)


class CiResultRequest(StrictBody):
    status: PipelineStatus
    artifactDigest: str | None = None
    logLines: list[str] = Field(default_factory=list, max_length=1000)


class ApprovalRequest(StrictBody):
    # No actor field: the approver is the authenticated principal. A body-supplied actor
    # would be a claim the server has no way to check, and an audit trail of claims is
    # not an audit trail.
    comment: str | None = None


class DeploymentResultRequest(StrictBody):
    status: Literal["healthy", "failed"]
    message: str | None = Field(default=None, max_length=2000)


class RollbackRequest(BaseModel):
    targetArtifactDigest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    reason: str = Field(min_length=3)


class SystemCreate(StrictBody):
    id: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9-]{2,62}$")
    unit: str = Field(min_length=2, max_length=200)
    description: str = Field(min_length=2, max_length=1000)


class HealthCheckSettings(StrictBody):
    script: str = Field(min_length=1, max_length=4000)
    retries: int = Field(default=3, ge=1, le=20)
    delay: str = Field(default="10s", pattern=r"^\d+(ms|s|m)$")


class ModuleTaskSettings(StrictBody):
    healthCheck: HealthCheckSettings | None = None


class ModulePipelineTabConfig(StrictBody):
    branch: str = Field(min_length=1, max_length=500)
    coverageReportPath: str = Field(min_length=1, max_length=500)
    stages: list[str] = Field(default_factory=list, min_length=1, max_length=100)


class ModulePipelineConfig(StrictBody):
    runner: str = Field(min_length=1, max_length=255)
    strategy: Literal["Gitflow", "Trunk-based", "Custom Pipeline"]
    pipelines: dict[str, ModulePipelineTabConfig] = Field(min_length=1, max_length=10)


class ModuleEnvironmentCreate(StrictBody):
    displayName: str = Field(min_length=1, max_length=120)
    environment: Environment
    runtime: Runtime
    servers: list[str] = Field(default_factory=list, max_length=200)
    tasks: list[str] = Field(default_factory=list, max_length=100)
    taskSettings: ModuleTaskSettings = Field(default_factory=ModuleTaskSettings)
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
    pipelineConfig: ModulePipelineConfig | None = None
    deploymentEnvironments: list[ModuleEnvironmentCreate] = Field(min_length=1, max_length=3)
    ownerTeam: str | None = Field(default=None, min_length=1, max_length=255)

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


class PortalApprovalRequest(StrictBody):
    comment: str | None = Field(default=None, max_length=1000)


class ModuleUpdate(StrictBody):
    displayName: str = Field(min_length=2, max_length=255)
    moduleType: str = Field(min_length=2, max_length=64)
    description: str = Field(default="", max_length=1000)


class ProductionRequestModuleCreate(StrictBody):
    moduleId: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{2,62}$")
    version: str = Field(pattern=r"^v?\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")
    deploymentOrder: int = Field(ge=1, le=100)


class ProductionRequestCreate(StrictBody):
    modules: list[ProductionRequestModuleCreate] = Field(min_length=1, max_length=1)
    # requestedBy is the authenticated principal; see ApprovalRequest.
    scheduledFor: datetime
    rollbackStrategy: Literal["automatic", "manual"] = "automatic"
    runAutomationTests: bool = True

    @model_validator(mode="after")
    def validate_modules(self) -> "ProductionRequestCreate":
        module_ids = [item.moduleId for item in self.modules]
        if len(module_ids) != len(set(module_ids)):
            raise ValueError("production request modules must be unique")
        if self.scheduledFor.tzinfo is None:
            raise ValueError("scheduledFor must include a timezone offset")
        return self


class VersionCreate(StrictBody):
    tag: str = Field(pattern=r"^v?\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")
    gitTagUrl: HttpUrl
    artifactUrl: HttpUrl
    pipelineRunId: UUID | None = None
    artifactDigest: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")


class SbomEvidence(BaseModel):
    generatedBy: Literal["syft"]
    location: str = Field(min_length=1, max_length=1000)
    format: str = Field(default="cyclonedx-json", max_length=64)


class VulnerabilityFinding(BaseModel):
    """One blocking finding, by identifier.

    A vulnerability exception waives a *named* CVE on a named digest, so evidence that
    reports only counts cannot be waived at all -- there is nothing to match against.
    The package and fixed version travel with it for whoever has to triage the finding.
    """

    id: str = Field(min_length=1, max_length=128)
    severity: Literal["HIGH", "CRITICAL"]
    package: str = Field(default="", max_length=255)
    installedVersion: str = Field(default="", max_length=128)
    fixedVersion: str = Field(default="", max_length=128)


class VulnerabilityScanEvidence(BaseModel):
    scanner: Literal["trivy"]
    status: Literal["passed", "failed"]
    critical: int = Field(default=0, ge=0)
    high: int = Field(default=0, ge=0)
    medium: int = Field(default=0, ge=0)
    # Declared, because pydantic drops what it does not declare. An undeclared field here
    # is not a harmless omission: the counts would arrive and the identifiers would not,
    # and every vulnerability exception would silently fail to apply.
    findings: list[VulnerabilityFinding] = Field(default_factory=list, max_length=500)
    reportLocation: str | None = Field(default=None, max_length=1000)


class SignatureEvidence(BaseModel):
    provider: Literal["cosign"]
    verified: bool
    certificateIdentity: str | None = Field(default=None, max_length=500)
    bundleLocation: str | None = Field(default=None, max_length=1000)


class SecurityEvidenceRequest(BaseModel):
    """Supply-chain evidence a CI run publishes for one immutable artifact."""

    artifactDigest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    artifactRef: str | None = Field(default=None, max_length=1000)
    sbom: SbomEvidence
    vulnerabilityScan: VulnerabilityScanEvidence
    signature: SignatureEvidence
    buildRunId: str | None = Field(default=None, max_length=255)


class VulnerabilityCounts(StrictBody):
    critical: int = Field(default=0, ge=0)
    high: int = Field(default=0, ge=0)
    medium: int = Field(default=0, ge=0)


class VersionCiReport(StrictBody):
    coverage: float = Field(ge=0, le=100)
    autoTest: Literal["passed", "failed", "skipped"]
    sast: Literal["passed", "failed"]
    sastIssues: int = Field(ge=0)
    vulnerabilities: VulnerabilityCounts
    commit: str = Field(pattern=r"^[0-9a-fA-F]{7,64}$")


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    correlation_id = request.state.correlation_id
    detail_dict = exc.detail if isinstance(exc.detail, dict) else {"code": "HTTP_ERROR", "message": str(exc.detail)}
    return error(
        detail_dict.get("code", "HTTP_ERROR"),
        detail_dict.get("message", "request failed"),
        correlation_id,
        exc.status_code,
        dict(exc.headers) if exc.headers else None,
        detail=detail_dict if isinstance(exc.detail, dict) else None,
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """Answer a rejected body.

    A refused build input keeps its own code and message. "request validation failed"
    would be true but useless: the caller needs to be told that it named a key the server
    owns, not left to guess which of its fields was wrong -- and a silent drop would teach
    it that the override had worked.
    """

    for item in exc.errors():
        cause = item.get("ctx", {}).get("error") if isinstance(item.get("ctx"), dict) else None
        if isinstance(cause, BuildInputError):
            return error(cause.code, cause.message, request.state.correlation_id, 422)
    return error("VALIDATION_ERROR", "request validation failed", request.state.correlation_id, 422)


@app.exception_handler(DeliveryError)
async def delivery_exception_handler(request: Request, exc: DeliveryError) -> JSONResponse:
    return error(exc.code, exc.message, request.state.correlation_id, exc.status_code)


@app.exception_handler(PortalError)
async def portal_exception_handler(request: Request, exc: PortalError) -> JSONResponse:
    return error(exc.code, exc.message, request.state.correlation_id, exc.status_code)


@app.get("/healthz")
def healthz(response: Response) -> dict[str, object]:
    """Report liveness and the state of each configured dependency.

    A configured-but-unreachable database is reported as `degraded` with 503, not as
    `ok`. Reporting healthy while writes would fail is precisely the false-green this
    platform exists to prevent, and a load balancer needs to see it too.
    """

    portal_health = portal.persistence_health()
    delivery_health = platform.persistence_health()
    degraded = delivery_health.startswith("unavailable") or portal_health.get("status") == "degraded"
    if degraded:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {
        "status": "degraded" if degraded else "ok",
        "version": "0.1.0",
        "engines": {
            "ci": getattr(platform.ci_launcher, "mode", "unknown"),
            "cd": getattr(platform.cd_orchestrator, "mode", "unknown"),
            # Reported so an operator can see from the outside that a deployment is
            # running without authentication, instead of discovering it from an incident.
            "auth": getattr(authenticator, "mode", "unknown"),
            # Enforced by the Temporal worker, not by this process; reported here because
            # this is where an operator looks, and "are we re-verifying signatures?" is
            # not a question anyone should have to answer by reading a worker's env.
            "signatureVerification": os.getenv("NETCI_SIGNATURE_VERIFY_MODE", "none").strip().lower() or "none",
            "rateLimit": (
                f"{rate_limiter.limit}/{int(rate_limiter.window_seconds)}s" if rate_limiter.enabled else "off"
            ),
        },
        "dependencies": {
            "portalPersistence": portal_health,
            "deliveryPersistence": delivery_health,
        },
    }


@app.get("/me")
def whoami(principal: Principal = Depends(current_principal)) -> dict[str, object]:
    """Who the presented credential belongs to, and what it may do.

    The Portal calls this to turn a token into a session, so the browser never decides
    who the user is or which controls to enable -- the server does, and the same answer
    is what every other endpoint enforces.
    """

    return {
        "principal": principal.as_json(),
        "authMode": getattr(authenticator, "mode", "unknown"),
        "separationOfDuties": separation_of_duties_enabled(principal),
    }


@app.get("/stage-catalog")
def stage_catalog(_: Principal = ReadAccess) -> dict[str, list[dict[str, object]]]:
    return platform.stage_catalog()


@app.get("/portal/dashboard")
def portal_dashboard(principal: Principal = ReadAccess) -> dict[str, object]:
    return portal.dashboard(_visible_application_ids(principal))


@app.get("/dcim/services")
def search_dcim_services(query: str = "", _: Principal = ReadAccess) -> dict[str, object]:
    try:
        return portal.dcim_services(query)
    except DcimUnavailable as exc:
        raise HTTPException(status_code=503, detail={"code": "DCIM_UNAVAILABLE", "message": str(exc)}) from exc


@app.get("/dcim/modules")
def list_dcim_modules(systemId: str, _: Principal = ReadAccess) -> dict[str, object]:
    try:
        return portal.dcim_modules(systemId)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail={"code": "SYSTEM_NOT_FOUND", "message": str(exc)}) from exc
    except DcimUnavailable as exc:
        raise HTTPException(status_code=503, detail={"code": "DCIM_UNAVAILABLE", "message": str(exc)}) from exc


@app.get("/dcim/servers")
def list_dcim_servers(
    systemId: str,
    moduleId: str | None = None,
    _: Principal = ReadAccess,
) -> dict[str, object]:
    try:
        return portal.dcim_servers(systemId, moduleId)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail={"code": "SYSTEM_NOT_FOUND", "message": str(exc)}) from exc
    except DcimUnavailable as exc:
        raise HTTPException(status_code=503, detail={"code": "DCIM_UNAVAILABLE", "message": str(exc)}) from exc


@app.get("/systems")
def list_systems(principal: Principal = ReadAccess) -> list[dict[str, object]]:
    return portal.systems(_visible_application_ids(principal))


@app.post("/systems", status_code=status.HTTP_201_CREATED)
def create_system(payload: SystemCreate, principal: Principal = DeveloperAccess) -> dict[str, object]:
    try:
        return portal.create_system(
            system_id=payload.id,
            unit=payload.unit,
            description=payload.description,
            owner=principal.subject,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail={"code": "SYSTEM_EXISTS", "message": str(exc)}) from exc


@app.get("/systems/{systemId}")
def get_system(systemId: str, principal: Principal = ReadAccess) -> dict[str, object]:
    try:
        unfiltered = portal.system(systemId)
        visible = portal.system(systemId, _visible_application_ids(principal))
        if unfiltered["modules"] and not visible["modules"]:
            raise HTTPException(
                status_code=403,
                detail={"code": "APPLICATION_FORBIDDEN", "message": "system has no modules accessible to this principal"},
            )
        return visible
    except KeyError as exc:
        raise HTTPException(status_code=404, detail={"code": "SYSTEM_NOT_FOUND", "message": str(exc)}) from exc


@app.delete("/systems/{systemId}", status_code=status.HTTP_204_NO_CONTENT)
def delete_system(systemId: str, principal: Principal = DeveloperAccess):
    try:
        for module in portal.system(systemId).get("modules") or []:
            _require_module_access(str(module["id"]), principal)
        portal.remove_system(systemId)
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail={"code": "SYSTEM_NOT_FOUND", "message": str(exc)}) from exc
    except PortalError as exc:
        # A refused delete has to reach the caller. Reporting 204 while the row survives
        # is a delete the user is told worked, that comes back on the next restart.
        raise HTTPException(status_code=exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc


@app.delete("/modules/{moduleId}", status_code=status.HTTP_204_NO_CONTENT)
def delete_module(moduleId: str, principal: Principal = DeveloperAccess):
    try:
        _require_module_access(moduleId, principal)
        portal.remove_module(moduleId)
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail={"code": "MODULE_NOT_FOUND", "message": str(exc)}) from exc
    except PortalError as exc:
        # A refused delete has to reach the caller. Reporting 204 while the row survives
        # is a delete the user is told worked, that comes back on the next restart.
        raise HTTPException(status_code=exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc


@app.get("/systems/{systemId}/modules")
def list_system_modules(systemId: str, principal: Principal = ReadAccess) -> list[dict[str, object]]:
    try:
        unfiltered = portal.system(systemId)
        visible = portal.system(systemId, _visible_application_ids(principal))
        if unfiltered["modules"] and not visible["modules"]:
            raise HTTPException(
                status_code=403,
                detail={"code": "APPLICATION_FORBIDDEN", "message": "system has no modules accessible to this principal"},
            )
        return list(visible["modules"])
    except KeyError as exc:
        raise HTTPException(status_code=404, detail={"code": "SYSTEM_NOT_FOUND", "message": str(exc)}) from exc


@app.post("/systems/{systemId}/modules", status_code=status.HTTP_201_CREATED)
def create_module(
    systemId: str,
    payload: ModuleCreate,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", min_length=1, max_length=128),
    principal: Principal = DeveloperAccess,
) -> dict[str, object]:
    owner_team = _application_owner_for_create(payload.ownerTeam, principal)
    try:
        # One transaction for the whole aggregate. The delivery application, the Portal
        # module and the idempotency record commit together or not at all, so a failure
        # here -- or a client that times out and retries -- can never leave an application
        # that no module points at.
        with platform.transaction() as transaction:
            application = platform.create_application(
                name=payload.name,
                repository_url=str(payload.repositoryUrl),
                pipeline_template=payload.pipelineTemplate,
                runtime=payload.runtime,
                default_environment=payload.defaultEnvironment,
                stages=payload.stages,
                idempotency_key=idempotency_key,
                owner_team=owner_team,
                session=transaction,
            )
            existing = transaction.portal_module_for_application(application.id)
            if existing is not None:
                # The idempotency record replayed the application this key created, and
                # the module written in that same transaction is still there. This is the
                # retry-after-timeout case: answer with the resource, not MODULE_EXISTS.
                return portal.module(existing.id, session=transaction)
            return portal.attach_module(
                system_id=systemId,
                module_id=payload.name,
                name=payload.displayName or payload.name,
                module_type=payload.moduleType,
                description=payload.description,
                runtime=payload.runtime,
                application_id=application.id,
                deployment_environments=[
                    item.model_dump(mode="json") for item in payload.deploymentEnvironments
                ],
                pipeline_config=payload.pipelineConfig.model_dump(mode="json")
                if payload.pipelineConfig
                else {},
                session=transaction,
            )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail={"code": "SYSTEM_NOT_FOUND", "message": str(exc)}) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail={"code": "MODULE_EXISTS", "message": str(exc)}) from exc


@app.get("/modules/{moduleId}")
def get_module(moduleId: str, principal: Principal = ReadAccess) -> dict[str, object]:
    try:
        return _require_module_access(moduleId, principal)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail={"code": "MODULE_NOT_FOUND", "message": str(exc)}) from exc


@app.patch("/modules/{moduleId}")
def update_module(moduleId: str, payload: ModuleUpdate, principal: Principal = DeveloperAccess) -> dict[str, object]:
    try:
        _require_module_access(moduleId, principal)
        return portal.update_module(
            moduleId,
            name=payload.displayName,
            module_type=payload.moduleType,
            description=payload.description,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail={"code": "MODULE_NOT_FOUND", "message": str(exc)}) from exc


@app.get("/modules/{moduleId}/overview")
def get_module_overview(moduleId: str, principal: Principal = ReadAccess) -> dict[str, object]:
    try:
        _require_module_access(moduleId, principal)
        return portal.module_overview(moduleId)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail={"code": "MODULE_NOT_FOUND", "message": str(exc)}) from exc


@app.post("/modules/{moduleId}/pipeline-runs", status_code=status.HTTP_202_ACCEPTED)
def start_module_pipeline_run(
    moduleId: str,
    payload: PipelineRunCreate,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", min_length=1, max_length=128),
    principal: Principal = PipelineStartAccess,
) -> dict[str, object]:
    _require_environment_role(payload.environment, principal)
    try:
        module = portal.module(moduleId)
        application_id = module.get("applicationId")
        if not application_id:
            raise DeliveryError("MODULE_NOT_PROVISIONED", "module has no delivery application", 409)
        _require_application_access(platform.get_application(UUID(str(application_id))), principal)
        run = platform.start_pipeline(
            UUID(str(application_id)),
            commit_sha=payload.commitSha,
            branch=payload.branch,
            environment=payload.environment,
            parameters=portal.delivery_parameters(moduleId, payload.environment, payload.parameters),
            correlation_id=request.state.correlation_id,
            idempotency_key=idempotency_key,
            started_by=principal.subject,
        )
        return pipeline_json(run)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail={"code": "MODULE_NOT_FOUND", "message": str(exc)}) from exc
    except PortalError as exc:
        raise HTTPException(status_code=exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc


@app.get("/modules/{moduleId}/pipeline-runs")
def list_module_pipeline_runs(moduleId: str, principal: Principal = ReadAccess) -> dict[str, object]:
    try:
        _require_module_access(moduleId, principal)
        return portal.pipeline_runs(moduleId)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail={"code": "MODULE_NOT_FOUND", "message": str(exc)}) from exc


@app.get("/modules/{moduleId}/versions")
def list_module_versions(moduleId: str, principal: Principal = ReadAccess) -> dict[str, object]:
    try:
        _require_module_access(moduleId, principal)
        return portal.versions(moduleId)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail={"code": "MODULE_NOT_FOUND", "message": str(exc)}) from exc


@app.post("/modules/{moduleId}/versions", status_code=status.HTTP_201_CREATED)
def create_module_version(
    moduleId: str,
    payload: VersionCreate,
    principal: Principal = Depends(requires(Role.DEVELOPER, Role.PLATFORM_ADMIN, Role.PIPELINE)),
) -> dict[str, object]:
    try:
        if not principal.has_any(Role.PIPELINE):
            _require_module_access(moduleId, principal)
        return portal.register_version(
            moduleId,
            tag=payload.tag,
            git_tag_url=str(payload.gitTagUrl),
            artifact_url=str(payload.artifactUrl),
            pipeline_run_id=payload.pipelineRunId,
            artifact_digest=payload.artifactDigest,
            created_by=principal.subject,
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
    _: Principal = PipelineAccess,
) -> dict[str, object]:
    try:
        return portal.record_ci_report(moduleId, tag, payload.model_dump())
    except KeyError as exc:
        raise HTTPException(status_code=404, detail={"code": "MODULE_NOT_FOUND", "message": str(exc)}) from exc


@app.get("/modules/{moduleId}/dora")
def get_module_dora(moduleId: str, principal: Principal = ReadAccess) -> dict[str, object]:
    try:
        _require_module_access(moduleId, principal)
        return portal.dora(moduleId)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail={"code": "MODULE_NOT_FOUND", "message": str(exc)}) from exc


@app.get("/systems/{systemId}/dora")
def get_system_dora(systemId: str, principal: Principal = ReadAccess) -> dict[str, object]:
    try:
        unfiltered = portal.system(systemId)
        visible = portal.system(systemId, _visible_application_ids(principal))
        if unfiltered["modules"] and not visible["modules"]:
            raise HTTPException(
                status_code=403,
                detail={"code": "APPLICATION_FORBIDDEN", "message": "system has no modules accessible to this principal"},
            )
        application_ids = [UUID(str(module["applicationId"])) for module in visible["modules"] if module.get("applicationId")]
        return {"scope": "system", "scopeId": systemId, **portal.dora_projection(application_ids)}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail={"code": "SYSTEM_NOT_FOUND", "message": str(exc)}) from exc


@app.get("/production-requests")
def list_production_requests(principal: Principal = ReadAccess) -> list[dict[str, object]]:
    visible: list[dict[str, object]] = []
    for request in portal.production_requests():
        try:
            for module in request.get("modules") or []:
                _require_module_access(str(module["moduleId"]), principal)
        except HTTPException as exc:
            if exc.status_code == 403:
                continue
            raise
        visible.append(request)
    return visible


@app.post("/production-requests", status_code=status.HTTP_201_CREATED)
def create_production_request(
    payload: ProductionRequestCreate,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", min_length=1, max_length=128),
    principal: Principal = DeveloperAccess,
) -> dict[str, object]:
    try:
        for requested_module in payload.modules:
            module = portal.module(requested_module.moduleId)
            application_id = module.get("applicationId")
            if application_id:
                _require_application_access(platform.get_application(UUID(str(application_id))), principal)
        # requested_by comes from the verified identity, never from the body: it is the
        # half of the separation-of-duties check that the approver is compared against.
        return portal.create_production_request(
            modules=[item.model_dump() for item in payload.modules],
            requested_by=principal.subject,
            scheduled_for=payload.scheduledFor,
            rollback_strategy=payload.rollbackStrategy,
            run_automation_tests=payload.runAutomationTests,
            idempotency_key=idempotency_key,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail={"code": "MODULE_NOT_FOUND", "message": str(exc)}) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail={"code": "VERSION_NOT_AVAILABLE", "message": str(exc)}) from exc
    except PortalError as exc:
        raise HTTPException(status_code=exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc


@app.post("/production-requests/{requestId}/approve", status_code=status.HTTP_202_ACCEPTED)
def approve_production_request(
    requestId: str, payload: PortalApprovalRequest, principal: Principal = ReviewerAccess
) -> dict[str, object]:
    existing = portal.production_request(requestId)
    if existing is None:
        raise HTTPException(
            status_code=404, detail={"code": "REQUEST_NOT_FOUND", "message": "production request not found"}
        )
    for requested_module in existing.get("modules") or []:
        module = portal.module(str(requested_module["moduleId"]))
        application_id = module.get("applicationId")
        if application_id:
            _require_application_access(platform.get_application(UUID(str(application_id))), principal)
    if separation_of_duties_enabled(principal):
        try:
            require_separation_of_duties(str(existing.get("requestedBy") or ""), principal.subject)
        except PolicyViolation as exc:
            raise HTTPException(
                status_code=403, detail={"code": "SEPARATION_OF_DUTIES", "message": str(exc)}
            ) from exc
    try:
        return portal.approve_request(requestId, principal.subject, payload.comment)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail={"code": "REQUEST_NOT_FOUND", "message": str(exc)}) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail={"code": "REQUEST_STATE_INVALID", "message": str(exc)}) from exc
    except PortalError as exc:
        raise HTTPException(status_code=exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc


@app.post("/production-requests/{requestId}/reject", status_code=status.HTTP_202_ACCEPTED)
def reject_production_request(
    requestId: str, payload: PortalApprovalRequest, principal: Principal = ReviewerAccess
) -> dict[str, object]:
    try:
        existing = portal.production_request(requestId)
        if existing is None:
            raise KeyError("production request not found")
        for requested_module in existing.get("modules") or []:
            _require_module_access(str(requested_module["moduleId"]), principal)
        return portal.reject_request(requestId, principal.subject, payload.comment)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail={"code": "REQUEST_NOT_FOUND", "message": str(exc)}) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail={"code": "REQUEST_STATE_INVALID", "message": str(exc)}) from exc


@app.get("/servers")
def list_servers(principal: Principal = ReadAccess) -> list[dict[str, object]]:
    return portal.servers(_visible_application_ids(principal))


@app.get("/audit-events")
def list_audit_events(systemId: str | None = None, moduleId: str | None = None, principal: Principal = ReadAccess) -> list[dict[str, object]]:
    try:
        return portal.audit_events(
            system_id=systemId,
            module_id=moduleId,
            application_ids=_visible_application_ids(principal),
        )
    except KeyError as exc:
        raise HTTPException(
            status_code=404,
            detail={"code": "AUDIT_SCOPE_NOT_FOUND", "message": str(exc)},
        ) from exc


@app.post("/applications", status_code=status.HTTP_201_CREATED, response_model=None)
def create_application(
    payload: ApplicationCreate,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", min_length=1, max_length=128),
    principal: Principal = DeveloperAccess,
) -> dict[str, object]:
    # You may hand an application to a team you belong to, and no other. Otherwise
    # ownership would be a field anyone could set to anything, which is not a control.
    owner_team = _application_owner_for_create(payload.ownerTeam, principal)
    application = platform.create_application(
        name=payload.name,
        repository_url=str(payload.repositoryUrl),
        pipeline_template=payload.pipelineTemplate,
        runtime=payload.runtime,
        default_environment=payload.defaultEnvironment,
        stages=payload.stages,
        idempotency_key=idempotency_key,
        owner_team=owner_team,
    )
    return application_json(application)


@app.get("/applications")
def list_applications(principal: Principal = ReadAccess) -> list[dict[str, object]]:
    return [
        application_json(item)
        for item in platform.list_applications()
        if _can_access_application(item, principal)
    ]


@app.get("/applications/{applicationId}/dora")
def get_application_dora(applicationId: UUID, principal: Principal = ReadAccess) -> dict[str, object]:
    """DORA for one delivery application, in the api/dora-dashboard.schema.json shape.

    `sourceEvents` is the number of durable delivery events the metrics were
    projected from, so a dashboard figure can always be traced back to its input.
    """

    _require_application_access(platform.get_application(applicationId), principal)
    projection = portal.dora_projection([applicationId])
    window = projection["window"]
    assert isinstance(window, dict)
    values = {str(item["key"]): float(item["value"]) for item in projection["metrics"]}  # type: ignore[index,union-attr]
    return {
        "applicationId": str(applicationId),
        "period": {"from": window["from"], "to": window["to"]},
        "metrics": {
            "deploymentFrequency": values["deploymentFrequency"],
            "changeLeadTimeSecondsAvg": round(values["leadTime"] * 3600, 3),
            "changeFailRate": round(values["changeFailureRate"] / 100, 6),
            "timeToRestoreServiceSecondsAvg": round(values["timeToRestoreService"] * 3600, 3),
        },
        "sourceEvents": projection["sourceEventCount"],
    }


@app.get("/delivery-events")
def list_delivery_events(applicationId: UUID | None = None, principal: Principal = ReadAccess) -> dict[str, object]:
    """Raw source events behind every DORA number, for evidence collection."""

    if applicationId is not None:
        _require_application_access(platform.get_application(applicationId), principal)
        events = platform.delivery_events(applicationId)
    else:
        visible_ids = _visible_application_ids(principal)
        events = tuple(event for event in platform.delivery_events() if event.application_id in visible_ids)
    return {"count": len(events), "items": [delivery_event_json(item) for item in events]}


@app.post("/applications/{applicationId}/pipeline-runs", status_code=status.HTTP_202_ACCEPTED, response_model=None)
def start_pipeline_run(
    applicationId: UUID,
    payload: PipelineRunCreate,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", min_length=1, max_length=128),
    principal: Principal = LowLevelPipelineAccess,
) -> JSONResponse | dict[str, object]:
    """Start a run against a delivery application directly.

    This is the low-level entry point: it names an application rather than a module, so it
    bypasses the Portal lookup that binds a run to the module's registered deployment
    target. It is therefore restricted to platform administrators and machine identities;
    developers start runs through `/modules/{moduleId}/pipeline-runs`, which resolves the
    server-owned target for them. The same build-input policy applies to both.
    """

    _require_environment_role(payload.environment, principal)
    _require_application_access(platform.get_application(applicationId), principal)
    run = platform.start_pipeline(
        applicationId,
        commit_sha=payload.commitSha,
        branch=payload.branch,
        environment=payload.environment,
        parameters=payload.parameters,
        correlation_id=request.state.correlation_id,
        idempotency_key=idempotency_key,
        started_by=principal.subject,
    )
    return pipeline_json(run)


@app.post("/pipeline-runs/{pipelineRunId}/callback-token", status_code=status.HTTP_201_CREATED)
def issue_pipeline_callback_token(
    pipelineRunId: UUID,
    payload: CallbackTokenRequest,
    principal: Principal = AdminAccess,
) -> dict[str, object]:
    """Mint a callback token bound to one pipeline run.

    Normally the CI launcher mints this in-process when it dispatches a build; the
    endpoint exists so an operator can re-issue one for a run whose token expired, and so
    the flow is testable end to end. It is platform-admin only: anyone who can mint a
    token for a run can report that run's result.

    The token is returned once and never stored. netCI keeps only its `jti`.
    """

    run = platform.get_pipeline(pipelineRunId)
    _require_application_access(platform.get_application(run.application_id), principal)
    try:
        token = workload_identity.mint(
            workload=payload.workload,
            application_id=run.application_id,
            pipeline_run_id=pipelineRunId,
            scopes=set(payload.scopes),
            ttl_seconds=payload.ttlSeconds,
        )
    except WorkloadIdentityError as exc:
        raise HTTPException(
            status_code=exc.status, detail={"code": exc.code, "message": exc.message}
        ) from exc
    return {
        "token": token,
        "workload": payload.workload,
        "pipelineRunId": str(pipelineRunId),
        "scopes": sorted(payload.scopes),
        "expiresInSeconds": payload.ttlSeconds,
    }


@app.get("/pipeline-runs/{pipelineRunId}")
def get_pipeline_run(pipelineRunId: UUID, principal: Principal = ReadAccess) -> dict[str, object]:
    run = platform.get_pipeline(pipelineRunId)
    _require_application_access(platform.get_application(run.application_id), principal)
    return pipeline_json(run)


@app.get("/pipeline-runs/{pipelineRunId}/logs")
def get_pipeline_logs(pipelineRunId: UUID, principal: Principal = ReadAccess) -> dict[str, object]:
    run, lines = platform.get_pipeline_logs(pipelineRunId)
    _require_application_access(platform.get_application(run.application_id), principal)
    return {"pipelineRunId": str(pipelineRunId), "correlationId": run.correlation_id, "lines": list(lines)}


@app.post("/pipeline-runs/{pipelineRunId}/ci-result", status_code=status.HTTP_202_ACCEPTED)
def record_ci_result(
    pipelineRunId: UUID,
    payload: CiResultRequest,
    request: Request,
    _: Principal = PipelineAccess,
) -> dict[str, object]:
    _authorize_callback(
        request, scope=Scope.CI_RESULT, workload=Workload.JENKINS, pipeline_run_id=pipelineRunId
    )
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


@app.post("/pipeline-runs/{pipelineRunId}/security-evidence", status_code=status.HTTP_202_ACCEPTED)
def publish_security_evidence(
    pipelineRunId: UUID,
    payload: SecurityEvidenceRequest,
    request: Request,
    _: Principal = PipelineAccess,
) -> dict[str, object]:
    """CI publishes SBOM, scan and signature evidence; netCI returns the policy verdict.

    The verdict is advisory here and binding at deploy time: `record_ci_result`
    re-evaluates the same rules before any deployment is created.
    """

    _authorize_callback(request, scope=Scope.CI_EVIDENCE, pipeline_run_id=pipelineRunId)
    decision = platform.record_security_evidence(pipelineRunId, payload.model_dump(mode="json"))
    return {"pipelineRunId": str(pipelineRunId), **decision.as_json()}


@app.get("/pipeline-runs/{pipelineRunId}/security-evidence")
def get_security_evidence(
    pipelineRunId: UUID, request: Request, principal: Principal = ReadAccess
) -> dict[str, object]:
    run = platform.get_pipeline(pipelineRunId)
    # The Temporal worker is the only machine reader: it re-verifies the artifact just
    # before deploy. Human readers remain team-scoped.
    if not principal.has_any(Role.PIPELINE):
        _require_application_access(platform.get_application(run.application_id), principal)
    else:
        _authorize_callback(request, scope=Scope.CI_EVIDENCE, pipeline_run_id=pipelineRunId)
    return platform.security_evidence(pipelineRunId)


@app.post("/deployments/{deploymentId}/approve", status_code=status.HTTP_202_ACCEPTED, response_model=None)
def approve_deployment(
    deploymentId: UUID, payload: ApprovalRequest, principal: Principal = ReviewerAccess
) -> JSONResponse | dict[str, object]:
    """Release a deployment that is waiting for a human.

    Three controls apply, in order: the caller must hold a reviewer role, production
    additionally requires that role by environment policy, and the approver must not be
    whoever started the run that produced this deployment.
    """

    deployment = platform.get_deployment(deploymentId)
    _require_environment_role(deployment.environment, principal)
    _require_application_access(platform.get_application(deployment.application_id), principal)
    if separation_of_duties_enabled(principal):
        try:
            require_separation_of_duties(platform.deployment_requested_by(deploymentId), principal.subject)
        except PolicyViolation as exc:
            raise HTTPException(
                status_code=403, detail={"code": "SEPARATION_OF_DUTIES", "message": str(exc)}
            ) from exc
    return deployment_json(platform.approve_deployment(deploymentId, principal.subject))


@app.get("/deployments/{deploymentId}")
def get_deployment(deploymentId: UUID, principal: Principal = ReadAccess) -> dict[str, object]:
    deployment = platform.get_deployment(deploymentId)
    _require_application_access(platform.get_application(deployment.application_id), principal)
    return deployment_json(deployment)


@app.post("/deployments/{deploymentId}/result", status_code=status.HTTP_202_ACCEPTED)
def record_deployment_result(
    deploymentId: UUID,
    payload: DeploymentResultRequest,
    request: Request,
    _: Principal = PipelineAccess,
) -> dict[str, object]:
    _authorize_callback(
        request,
        scope=Scope.DEPLOYMENT_RESULT,
        workload=Workload.TEMPORAL,
        deployment_id=deploymentId,
    )
    deployment = platform.record_deployment_result(deploymentId, payload.status, payload.message)
    portal.record_production_deployment_result(deploymentId, payload.status, payload.message)
    return deployment_json(deployment)


@app.post("/deployments/{deploymentId}/rollback", status_code=status.HTTP_202_ACCEPTED, response_model=None)
def rollback_deployment(
    deploymentId: UUID, payload: RollbackRequest, principal: Principal = ReviewerAccess
) -> JSONResponse | dict[str, object]:
    _require_application_access(
        platform.get_application(platform.get_deployment(deploymentId).application_id), principal
    )
    return deployment_json(
        platform.rollback_deployment(deploymentId, payload.targetArtifactDigest)
    )
