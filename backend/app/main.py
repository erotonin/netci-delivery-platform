from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import logging
import os
import re
import subprocess
import secrets
import tempfile
import time
import urllib.request

logger = logging.getLogger("netci.main")
from datetime import datetime, timedelta, timezone
from contextlib import asynccontextmanager
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response, status, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response as PlainResponse, StreamingResponse

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator, model_validator
from starlette.exceptions import HTTPException as StarletteHTTPException

from .adapters.cd_orchestrator import build_cd_orchestrator
from .adapters.ci_launcher import build_ci_launcher
from .adapters.dcim import (
    DatabaseServerState,
    DcimUnavailable,
    configure_server_state,
    get_server_telemetry as stored_server_telemetry,
    set_server_maintenance,
    update_server_telemetry,
)
from .auth import AuthError, Principal, build_authenticator
from .build_inputs import BuildInputError, validate_build_inputs
from .client_address import LOOPBACK_HOSTS, resolve_client
from .ratelimit import build_rate_limiter
from .domain.delivery_rules import (
    ScmEvent,
    decide as decide_trigger,
    default_rules as default_delivery_rules,
)
from .domain.models import (
    DeploymentStatus,
    Application,
    DeliveryEvent,
    Deployment,
    Environment,
    NotificationStatus,
    PipelineRun,
    PipelineStage,
    PipelineStatus,
    Runtime,
    ScmIntegration,
    ScmProviderType,
    ScmWebhookDelivery,
    SecurityWaiver,
    ServerMaintenanceState,
    WaiverStatus,
)
from .adapters.scm import MAX_WEBHOOK_PAYLOAD_BYTES, get_scm_provider
from .delivery import CiResult, DeliveryError, DeliveryPlatform
from .logging import current_correlation_id
from .metrics import PrometheusMetricsMiddleware, metrics
from .admission import AdmissionController
from .coordinator import ReleasePlanCoordinator
from .demo_data import seed_demo_data
from .notifications import NotificationOutboxWorker, build_outbox_dispatcher
from .policy.break_glass import BreakGlassError, BreakGlassService
from .policy.engine import PolicyEngine
from .runtime_environment import is_local_runtime
from .policy.rules import (
    PROD_CD_ROLES,
    PolicyViolation,
    Role,
    require_environment_permission,
    require_separation_of_duties,
    require_team_access,
)
from .portal import SERVER_OWNED_DELIVERY_KEYS, PortalError, PortalService
from .persistence import AuditRecord, UnitOfWork
from .readiness import probe_readiness, without_operator_detail
from .reconciler import Reconciler
from .retention import RetentionManager
from .adapters.trivy_rescan import build_sbom_scanner
from .adapters.prometheus_metrics import MetricsUnavailable, build_metrics_source
from .domain.verification import VerificationConfigError, VerificationResult, render_query, verdict_for_canary
from .exposure import SEVERITY_ORDER, exposure as vulnerability_exposure, rescan_in_service
from .store import build_database
from .store.records import (
    ArtifactFindingRecord,
    ArtifactSbomRecord,
    BreakGlassRecord,
    CatalogServiceRecord,
    CatalogTemplateRecord,
    ChangeFreezeRecord,
    PolicyDecisionRecord,
    PreviewEnvironmentRecord,
    ResourceQuotaRecord,
    ResourceRequestRecord,
    SecurityExceptionRecord,
)
from .catalog.services import CatalogServiceManager, CatalogValidationError
from .catalog.templates import PipelineTemplateEngine, TemplateValidationError, TemplateVersionConflict, seed_builtin_templates
from .catalog.previews import PreviewEnvironmentManager, PreviewEnvironmentError
from .catalog.resources import SelfServiceResourceManager, ResourceRequestError

from .traffic import TrafficRoutingUnavailable, default_traffic_router
from .agent_fleet import LOCK_RECONCILE, LOCK_RETENTION, LOCK_SBOM_RESCAN, AgentFleet, run_exclusively
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

# Composition root: the engines and the store are chosen here from configuration, never
# inside the domain. NETCI_CI_MODE / NETCI_CD_MODE may be "none" only in local mode; outside
# local their factories fail at startup instead of running a control plane that cannot
# execute the work it accepts. `build_database` refuses an in-memory store outside local
# mode for the same reason.
workload_identity.require_configured_workload_identity()
database = build_database()
# Maintenance mode and telemetry decide where a deployment may go; they are read by the
# DCIM catalogs through this binding and live in the same database as everything else.
configure_server_state(DatabaseServerState(database))
platform = DeliveryPlatform(build_ci_launcher(), build_cd_orchestrator(), database=database)
portal = PortalService(platform, database=database)
reconciler = Reconciler(platform, platform.ci_launcher, platform.cd_orchestrator)
sbom_scanner = build_sbom_scanner()
# The canary's metrics come from here, never from the request (ADR-046).
metrics_source = build_metrics_source()
# Which replica holds which edge agent, and the commands waiting for them, are rows in
# the same database (ADR-032); only the sockets themselves are process-local.
fleet = AgentFleet(database)
seed_demo_data(platform, portal)
authenticator = build_authenticator()
rate_limiter = build_rate_limiter()


@asynccontextmanager
async def lifespan(application: FastAPI):
    outbox_worker = NotificationOutboxWorker(database, dispatcher=build_outbox_dispatcher())
    if os.getenv("NETCI_DISABLE_OUTBOX_WORKER", "0") != "1":
        outbox_worker.start()
    # There is deliberately no simulated CI here. A worker that invented artifact digests
    # and CI reports for queued runs used to start whenever Jenkins was not configured;
    # it produced deployments for artifacts that did not exist and dashboards showing
    # successes that never happened. Local development runs the Jenkins lab.
    # A callback can be lost: a Jenkins build that fails at checkout never reaches the
    # pipeline's own report stages, and its run stayed `queued` forever on the live stack
    # until an operator called /reconciler/reconcile by hand. The reconciler compares
    # every active run and deployment with what Jenkins and Temporal actually say; it
    # runs on a schedule here so that the comparison does not depend on someone
    # remembering to make it.
    reconcile_task = asyncio.create_task(_reconcile_periodically())
    # Retention used to be an endpoint an operator had to remember; the tables that
    # only ever grow (console lines, delivery events, delivered notifications, spent
    # callback tokens) are now thinned on a schedule, one replica per pass.
    retention_task = asyncio.create_task(_purge_retention_periodically())
    rescan_task = asyncio.create_task(_rescan_sboms_periodically())
    yield
    for task in (reconcile_task, retention_task, rescan_task):
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    outbox_worker.stop()
    if hasattr(database, "close"):
        database.close()


def _retention_interval_seconds() -> float:
    raw = os.getenv("NETCI_RETENTION_INTERVAL_SECONDS", "86400").strip()
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 86400.0


def run_retention_pass() -> dict[str, int] | None:
    """One purge under the advisory lock; None when another replica holds it."""

    manager = RetentionManager.from_environment(database)
    outcome = run_exclusively(database, LOCK_RETENTION, manager.purge_all, describe="retention")
    if outcome:
        for kind, count in outcome.items():
            if count:
                metrics.counter_inc("netci_retention_purged_total", {"kind": kind}, float(count))
    return outcome


async def _purge_retention_periodically() -> None:
    interval = _retention_interval_seconds()
    if interval <= 0:
        logger.info("scheduled retention disabled (NETCI_RETENTION_INTERVAL_SECONDS=0)")
        return
    # The first pass soon after start (a restarted platform should not wait a day to
    # thin its tables), then every interval.
    delay = min(interval, 300.0)
    logger.info("scheduled retention every %.0fs (first pass in %.0fs)", interval, delay)
    while True:
        await asyncio.sleep(delay)
        delay = interval
        try:
            outcome = await asyncio.to_thread(run_retention_pass)
        except Exception:  # noqa: BLE001 - the loop must outlive one bad pass
            logger.exception("retention pass failed; will retry after %.0fs", interval)
            continue
        if outcome and any(outcome.values()):
            logger.info("retention pass purged %s", outcome)


def _sbom_rescan_interval_seconds() -> float:
    raw = os.getenv("NETCI_SBOM_RESCAN_INTERVAL_SECONDS", "21600").strip()
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 21600.0


async def _rescan_sboms_periodically() -> None:
    interval = _sbom_rescan_interval_seconds()
    if interval <= 0:
        logger.info("periodic SBOM rescanning disabled (NETCI_SBOM_RESCAN_INTERVAL_SECONDS=0)")
        return
    if sbom_scanner.name == "none":
        # Not "every artifact is clean": the exposure API reports each one as never rescanned.
        logger.info("SBOM rescanning not configured (NETCI_SBOM_RESCAN=none)")
        return
    delay = min(interval, 60.0)
    logger.info("periodic SBOM rescanning every %.0fs (first pass in %.0fs)", interval, delay)
    while True:
        await asyncio.sleep(delay)
        delay = interval
        try:
            outcome = await asyncio.to_thread(
                run_exclusively,
                database,
                LOCK_SBOM_RESCAN,
                lambda: rescan_in_service(platform, sbom_scanner),
                describe="sbom_rescan",
            )
        except Exception:  # noqa: BLE001 - the loop must outlive one bad pass
            logger.exception("SBOM rescan pass failed; will retry after %.0fs", interval)
            continue
        if outcome:
            logger.info("SBOM rescan pass completed: %s", outcome)


def _reconcile_interval_seconds() -> float:
    raw = os.getenv("NETCI_RECONCILE_INTERVAL_SECONDS", "60").strip()
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 60.0


async def _reconcile_periodically() -> None:
    interval = _reconcile_interval_seconds()
    if interval <= 0:
        logger.info("periodic reconciliation disabled (NETCI_RECONCILE_INTERVAL_SECONDS=0)")
        return
    logger.info("periodic reconciliation every %.0fs", interval)
    while True:
        await asyncio.sleep(interval)
        try:
            # With several API replicas, exactly one runs each pass (advisory lock); the
            # others skip it. Two reconcilers correcting the same run would each report
            # the correction and could race a callback that arrived between them.
            outcome = await asyncio.to_thread(
                run_exclusively, database, LOCK_RECONCILE, reconciler.reconcile, describe="reconciliation"
            )
        except Exception:  # noqa: BLE001 - the loop must outlive one bad pass
            logger.exception("reconciliation pass failed; will retry after %.0fs", interval)
            continue
        if outcome is None:
            continue
        runs = outcome.get("reconciledRuns") or []
        deployments = outcome.get("reconciledDeployments") or []
        if runs:
            metrics.counter_inc("netci_reconciler_corrections_total", {"kind": "run"}, float(len(runs)))
        if deployments:
            metrics.counter_inc("netci_reconciler_corrections_total", {"kind": "deployment"}, float(len(deployments)))
        if runs or deployments:
            logger.warning(
                "reconciled %d run(s) and %d deployment(s) whose callbacks never arrived: %s",
                len(runs), len(deployments), [(r.get("pipelineRunId"), r.get("action")) for r in runs][:10],
            )


app = FastAPI(title="netCI Delivery API", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=configured_cors_origins(),
    allow_credentials=True,
    allow_methods=["GET", "POST", "DELETE", "PUT", "PATCH", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "Idempotency-Key", "X-Correlation-Id"],
    expose_headers=["X-Correlation-Id"],
)
app.add_middleware(PrometheusMetricsMiddleware)


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

    token = current_correlation_id.set(correlation_id)
    try:
        # Identify the caller by credential where there is one, so a shared NAT does not make
        # a whole office look like a single client and throttle everyone because one person
        # looped. The token is hashed, never stored: the limiter's map would otherwise be a
        # list of live credentials sitting in memory.
        if rate_limiter.enabled and request.url.path not in ("/healthz", "/livez", "/readyz", "/metrics"):
            authorization = request.headers.get("Authorization", "")
            if authorization:
                caller = "credential:" + hashlib.sha256(authorization.encode()).hexdigest()[:32]
            else:
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
    finally:
        current_correlation_id.reset(token)

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
    if not supplied or not workload_identity.looks_like_callback_token(supplied):
        # Not ours. A Keycloak or Azure AD JWT is also three dot-separated parts, and
        # treating every such token as a callback token refused every human before the
        # OIDC authenticator ran. The header's `typ` says whose token it is.
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
def _reviewer_access(principal: Principal = Depends(current_principal)) -> Principal:
    # The same set the policy layer enforces, so the API cannot drift more permissive
    # than the rule it is meant to apply.
    allowed = PROD_CD_ROLES
    if principal.has_any(*allowed):
        return principal
    required = ", ".join(sorted(role.value for role in allowed))
    if principal.is_anonymous:
        raise HTTPException(
            status_code=401,
            detail={"code": "UNAUTHENTICATED", "message": f"this action requires one of: {required}"},
            headers={"WWW-Authenticate": "Bearer"},
        )
    held = ", ".join(sorted(role.value for role in principal.roles)) or "no roles"
    raise HTTPException(
        status_code=403,
        detail={"code": "FORBIDDEN", "message": f"this action requires one of: {required}; {principal.subject} holds: {held}"},
    )

ReviewerAccess = Depends(_reviewer_access)
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
    scope: str | tuple[str, ...] | set[str] | frozenset[str],
    workload: str | None = None,
    pipeline_run_id: UUID | None = None,
    deployment_id: UUID | None = None,
    terminal: bool = False,
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
    if isinstance(scope, (tuple, list, set, frozenset)):
        if not any(claims.permits(s) for s in scope):
            allowed = " or ".join(sorted(scope))
            raise HTTPException(
                status_code=403,
                detail={
                    "code": "SCOPE_NOT_PERMITTED",
                    "message": f"this callback token does not carry the {allowed} scope",
                },
            )
    elif not claims.permits(scope):
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
    if claims.single_use and terminal:
        # Single use applies to the terminal *operation* -- reporting a result -- not to
        # every call the token makes. The worker reads evidence and heartbeats with the
        # same token before it reports; spending the jti on the first of those made the
        # report itself look like a replay.
        # Claimed in its own transaction and by primary key, so two replicas handed the
        # same replayed token cannot both decide it was unused.
        claimed = _claim_single_use(claims, scope)
        if not claimed and not _deployment_already_terminal(claims.deployment_id):
            # A second use is a replay -- unless the deployment has already reached a
            # terminal state, in which case this is Temporal retrying a report that
            # already landed. The domain answers such a retry idempotently, refuses any
            # transition the state machine forbids, and the fencing token refuses a
            # superseded writer; single-use adds nothing there except breaking retries.
            raise HTTPException(
                status_code=401,
                detail={
                    "code": "TOKEN_REPLAYED",
                    "message": "this callback token has already been used",
                },
                headers={"WWW-Authenticate": "Bearer"},
            )
    return claims


def _deployment_already_terminal(deployment_id: UUID | None) -> bool:
    if deployment_id is None:
        return False
    try:
        current = platform.get_deployment(deployment_id)
    except DeliveryError:
        return False
    return current.status in {
        DeploymentStatus.HEALTHY,
        DeploymentStatus.FAILED,
        DeploymentStatus.ROLLED_BACK,
        DeploymentStatus.ROLLBACK_FAILED,
        DeploymentStatus.CANCELLED,
    }


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


def _exposure_application_ids(principal: Principal) -> set[UUID] | None:
    if principal.has_any(Role.PLATFORM_ADMIN):
        return None
    return _visible_application_ids(principal)


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
    return {
        "id": str(item.id),
        "applicationId": str(item.application_id),
        "status": item.status.value,
        "commitSha": item.commit_sha,
        "branch": item.branch,
        "environment": item.environment.value,
        "parameters": dict(item.parameters),
        "correlationId": item.correlation_id,
        "jenkinsRunId": item.jenkins_run_id,
        "workflowId": item.workflow_id,
        "artifactDigest": item.artifact_digest,
        "consoleUrl": item.console_url,
        "retryOf": str(item.retry_of) if item.retry_of else None,
        "configRevisionId": str(item.config_revision_id) if item.config_revision_id else None,
        "startedBy": item.started_by,
        "deployAfterBuild": item.deploy_after_build,
        "publishArtifact": item.publish_artifact,
        "releaseTag": item.release_tag,
        "trigger": dict(item.trigger),
        "createdAt": item.created_at.isoformat(),
        "updatedAt": item.updated_at.isoformat(),
    }


def stage_json(item: PipelineStage) -> dict[str, object]:
    return {
        "id": str(item.id),
        "pipelineRunId": str(item.pipeline_run_id),
        "stageId": item.stage_id,
        "stageName": item.stage_name,
        "status": item.status,
        "attempt": item.attempt,
        "queuedAt": item.queued_at.isoformat() if item.queued_at else None,
        "startedAt": item.started_at.isoformat() if item.started_at else None,
        "completedAt": item.completed_at.isoformat() if item.completed_at else None,
        "durationMs": item.duration_ms,
        "errorMessage": item.error_message,
        "logSnippet": item.log_snippet,
        "createdAt": item.created_at.isoformat(),
        "updatedAt": item.updated_at.isoformat(),
    }


def deployment_json(item: Deployment) -> dict[str, object]:
    return {
        "id": str(item.id),
        "applicationId": str(item.application_id),
        "pipelineRunId": str(item.pipeline_run_id) if item.pipeline_run_id else None,
        "runtime": item.runtime.value,
        "environment": item.environment.value,
        "status": item.status.value,
        "artifactDigest": item.artifact_digest,
        "previousArtifactDigest": item.previous_artifact_digest,
        "approvedBy": item.approved_by,
        "fencingToken": item.fencing_token,
        "configRevisionId": str(item.config_revision_id) if item.config_revision_id else None,
        "createdAt": item.created_at.isoformat(),
        "updatedAt": item.updated_at.isoformat(),
    }


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


def policy_decision_json(item: PolicyDecisionRecord) -> dict[str, object]:
    return {
        "id": str(item.id),
        "scope": item.scope,
        "targetType": item.target_type,
        "targetId": item.target_id,
        "allowed": item.allowed,
        "reason": item.reason,
        "riskScore": item.risk_score,
        "checks": item.checks,
        "rulesEvaluated": item.rules_evaluated,
        "evaluator": item.evaluator,
        "evaluatedAt": item.evaluated_at.isoformat(),
        "metadata": item.metadata,
    }


def security_exception_json(item: SecurityExceptionRecord) -> dict[str, object]:
    return {
        "id": str(item.id),
        "cve": item.cve,
        "artifactDigest": item.artifact_digest,
        "owner": item.owner,
        "reason": item.reason,
        "approvedBy": item.approved_by,
        "status": item.status,
        "createdAt": item.created_at.isoformat(),
        "expiresAt": item.expires_at.isoformat(),
        "revokedAt": item.revoked_at.isoformat() if item.revoked_at else None,
        "revokedBy": item.revoked_by,
    }


def break_glass_json(item: BreakGlassRecord) -> dict[str, object]:
    return {
        "id": str(item.id),
        "targetType": item.target_type,
        "targetId": item.target_id,
        "requestedBy": item.requested_by,
        "reason": item.reason,
        "incidentTicket": item.incident_ticket,
        "status": item.status,
        "approvedBy": item.approved_by,
        "createdAt": item.created_at.isoformat(),
        "approvedAt": item.approved_at.isoformat() if item.approved_at else None,
        "expiresAt": item.expires_at.isoformat() if item.expires_at else None,
    }


def resource_quota_json(item: ResourceQuotaRecord) -> dict[str, object]:
    return {
        "id": str(item.id),
        "scope": item.scope,
        "scopeId": item.scope_id,
        "maxConcurrentPipelines": item.max_concurrent_pipelines,
        "maxConcurrentDeployments": item.max_concurrent_deployments,
        "maxProductionRequestsPerDay": item.max_production_requests_per_day,
        "createdAt": item.created_at.isoformat(),
        "updatedAt": item.updated_at.isoformat(),
    }


class StrictBody(BaseModel):
    """Request body whose checked-in OpenAPI schema forbids undeclared fields."""

    model_config = ConfigDict(extra="forbid")


class CancelRequest(StrictBody):
    reason: str = Field(default="", max_length=256)


class StageResultRequest(StrictBody):
    stageId: str | None = Field(default=None, min_length=1, max_length=64)
    stageName: str | None = Field(default=None, max_length=128)
    status: str = Field(pattern=r"^(queued|running|succeeded|failed|cancelled|skipped)$")
    attempt: int = Field(default=1, ge=1)
    queuedAt: datetime | None = None
    startedAt: datetime | None = None
    completedAt: datetime | None = None
    durationMs: int | None = Field(default=None, ge=0)
    errorMessage: str | None = None
    logSnippet: str | None = None


class ReconcileRequest(StrictBody):
    limit: int = Field(default=50, ge=1, le=500)
    timeoutSeconds: int | None = Field(default=None, ge=1)


class ApplicationCreate(StrictBody):
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{2,62}$")
    # The team accountable for this application. Optional while ownership is being
    # adopted; required once NETCI_REQUIRE_APPLICATION_OWNER is set.
    ownerTeam: str | None = Field(default=None, max_length=255)
    repositoryUrl: HttpUrl
    pipelineTemplate: str
    runtime: Runtime
    defaultEnvironment: Environment = Environment.DEV
    stages: list[str] = Field(default_factory=list)


class ScmIntegrationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: ScmProviderType
    repositoryIdentity: str = Field(min_length=1, max_length=255)
    secretToken: str = Field(min_length=8, max_length=255)
    credentialReference: str | None = Field(default=None, max_length=128)
    enabled: bool = True


def scm_integration_json(item: ScmIntegration) -> dict[str, object]:
    # Redact secretToken and secretTokenHash: never exposed in API responses or logs
    return {
        "id": str(item.id),
        "applicationId": str(item.application_id),
        "provider": item.provider.value,
        "repositoryIdentity": item.repository_identity,
        "credentialReference": item.credential_reference,
        "enabled": item.enabled,
        "createdAt": item.created_at.isoformat(),
        "updatedAt": item.updated_at.isoformat(),
    }


class PipelineRunCreate(StrictBody):
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
    #: False builds, tests, signs and publishes the commit without deploying it; the
    #: artifact can then be promoted (ADR-043). `environment` still says whose build
    #: configuration applies and where a promotion of it would start.
    deploy: bool = True

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
    # The lease generation the reporting workflow was started under. netCI hands this to
    # the workflow when it starts it; sending it back is what lets a workflow that was
    # superseded be told apart from the one that replaced it.
    fencingToken: int | None = Field(default=None, ge=1)


class RollbackResultRequest(StrictBody):
    succeeded: bool
    message: str | None = Field(default=None, max_length=2000)
    fencingToken: int | None = Field(default=None, ge=1)


class HeartbeatRequest(StrictBody):
    fencingToken: int = Field(ge=1)


class RollbackRequest(StrictBody):
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


class RuntimeSettings(StrictBody):
    """How the checked-in playbook lays the service out on the target.

    These are the only playbook inputs a module may configure, and they live in the
    reviewed configuration revision rather than in a pipeline trigger: a caller who
    could pass `app_root` per run could point a deployment at any directory on the
    host. The playbooks own everything else (task order, health gate, rollback).
    """

    # Where the release is installed. Absolute, no traversal; the playbook still runs
    # under the worker's own Ansible privilege policy.
    appRoot: str | None = Field(default=None, min_length=2, max_length=255, pattern=r"^/[A-Za-z0-9._/-]+$")
    # Docker: port published on the host (bridge) or bound directly (host networking).
    hostPort: int | None = Field(default=None, ge=1024, le=65535)
    containerPort: int | None = Field(default=None, ge=1, le=65535)
    networkMode: Literal["bridge", "host"] | None = None
    # systemd: the port the unit listens on; the health gate derives its URL from it.
    appPort: int | None = Field(default=None, ge=1024, le=65535)
    systemdScope: Literal["system", "user"] | None = None
    # Whether Ansible escalates privilege on the target. A lab that must not be modified
    # sets this false and uses a user-writable appRoot and a user-scope systemd unit.
    become: bool | None = None
    # The name under which this target's container runtime reaches the registry recorded
    # in the artifact reference (a registry is often `registry.svc:5000` inside the build
    # cluster and something else on the host it deploys to). Only the locator changes:
    # the digest is the identity and the runtime verifies it on pull.
    imagePullHost: str | None = Field(default=None, min_length=1, max_length=253, pattern=r"^[A-Za-z0-9][A-Za-z0-9.-]*(:[0-9]{1,5})?$")

    @field_validator("appRoot")
    @classmethod
    def app_root_has_no_traversal(cls, value: str | None) -> str | None:
        if value is not None and ".." in value.split("/"):
            raise ValueError("appRoot must not contain '..' segments")
        return value


class ModulePipelineTabConfig(StrictBody):
    branch: str = Field(min_length=1, max_length=500)
    # Optional: a module without a coverage report is a fact the report shows as "no
    # coverage", not a reason to refuse the module.
    coverageReportPath: str | None = Field(default=None, max_length=500)
    stages: list[str] = Field(default_factory=list, min_length=1, max_length=100)


class ModulePipelineConfig(StrictBody):
    runner: str = Field(min_length=1, max_length=255)
    strategy: Literal["Gitflow", "Trunk-based", "Custom Pipeline"]
    pipelines: dict[str, ModulePipelineTabConfig] = Field(min_length=1, max_length=10)
    #: What SCM events cause and what promotions must prove (ADR-043). Validated by
    #: `domain.delivery_rules.parse_rules` against the module's environments; absent
    #: means the defaults.
    delivery: dict[str, Any] | None = None
    #: Build inputs every run of the module gets -- NETCI_APP_DIR for a monorepo above
    #: all -- held to the same boundary as a caller's (`app.build_inputs`).
    buildInputs: dict[str, Any] | None = None
    #: Post-deploy and canary verification queries and thresholds (ADR-046), validated by
    #: `domain.verification.parse_verification`.
    verification: dict[str, Any] | None = None


class ModuleEnvironmentCreate(StrictBody):
    displayName: str = Field(min_length=1, max_length=120)
    environment: Environment
    runtime: Runtime
    servers: list[str] = Field(default_factory=list, max_length=200)
    tasks: list[str] = Field(default_factory=list, max_length=100)
    taskSettings: ModuleTaskSettings = Field(default_factory=ModuleTaskSettings)
    kubeconfigRef: str | None = Field(default=None, min_length=1, max_length=255)
    namespace: str | None = Field(default=None, pattern=r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$", max_length=63)
    runtimeSettings: RuntimeSettings | None = None

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


class ModuleCreate(StrictBody):
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


class DeploymentTargetRevision(BaseModel):
    """One environment's target inside a proposed configuration revision.

    A revision becomes the module's active deployment configuration, so the fields the
    runtime adapter reads are validated with the same rules as module creation. Other
    keys are kept as-is: the risk classifier reads them, and they never reach a
    playbook -- `_delivery_parameters` picks fields by name.
    """

    model_config = ConfigDict(extra="allow")

    environment: Environment
    displayName: str | None = Field(default=None, min_length=1, max_length=120)
    runtime: Runtime | None = None
    servers: list[str] = Field(default_factory=list, max_length=200)
    tasks: list[str] = Field(default_factory=list, max_length=100)
    taskSettings: ModuleTaskSettings | None = None
    kubeconfigRef: str | None = Field(default=None, min_length=1, max_length=255)
    namespace: str | None = Field(default=None, pattern=r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$", max_length=63)
    runtimeSettings: RuntimeSettings | None = None

    @field_validator("servers")
    @classmethod
    def servers_are_inventory_names(cls, value: list[str]) -> list[str]:
        for host in value:
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,252}", host):
                raise ValueError(f"server {host!r} is not an inventory host name")
        return value


class ConfigRevisionCreate(StrictBody):
    changeSummary: str = Field(default="", max_length=1000)
    pipelineConfig: dict[str, object] = Field(default_factory=dict)
    deploymentConfig: list[DeploymentTargetRevision] = Field(default_factory=list, max_length=3)
    expectedVersion: int | None = None


class ConfigRevisionReject(StrictBody):
    reason: str = Field(min_length=1, max_length=1000)


class ConfigApplyRequest(StrictBody):
    environment: str = Field(default="prod", pattern=r"^(dev|staging|prod)$")
    revisionId: UUID | None = None
    fencingToken: int | None = None


class ProductionRequestModuleCreate(StrictBody):
    moduleId: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{2,62}$")
    version: str = Field(pattern=r"^v?\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")
    deploymentOrder: int = Field(default=1, ge=1, le=100)
    dependencies: list[str] = Field(default_factory=list)


class ProductionRequestCreate(StrictBody):
    modules: list[ProductionRequestModuleCreate] = Field(min_length=1, max_length=100)
    # requestedBy is the authenticated principal; see ApprovalRequest.
    scheduledFor: datetime
    rollbackStrategy: Literal["automatic", "manual"] = "automatic"
    runAutomationTests: bool = True
    strategy: Literal["rolling", "canary", "blue_green"] = "rolling"
    strategyConfig: dict[str, Any] = Field(default_factory=dict)
    canaryRules: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_modules(self) -> "ProductionRequestCreate":
        module_ids = [item.moduleId for item in self.modules]
        if len(module_ids) != len(set(module_ids)):
            raise ValueError("production request modules must be unique")
        if self.scheduledFor.tzinfo is None:
            raise ValueError("scheduledFor must include a timezone offset")
        return self


class SecurityWaiverCreate(StrictBody):
    cveId: str = Field(min_length=3, max_length=64)
    reason: str = Field(min_length=5, max_length=1000)
    expiresAt: datetime
    moduleId: str | None = None


class ServerMaintenanceRequest(StrictBody):
    inMaintenance: bool
    reason: str = Field(default="", max_length=1000)
    # No `updatedBy`: the actor is the authenticated principal. A body-supplied name is a
    # claim the server cannot check, and an audit trail of claims is not an audit trail.


class VersionCreate(StrictBody):
    tag: str = Field(pattern=r"^v?\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")
    # Links are optional: the artifact is identified by its digest and the run that
    # built it, which the server verifies. A link is a convenience for people.
    gitTagUrl: HttpUrl | None = None
    artifactUrl: HttpUrl | None = None
    pipelineRunId: UUID | None = None
    artifactDigest: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")


class SbomEvidence(StrictBody):
    generatedBy: Literal["syft"]
    location: str = Field(min_length=1, max_length=1000)
    format: str = Field(default="cyclonedx-json", max_length=64)


class VulnerabilityFinding(StrictBody):
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


class VulnerabilityScanEvidence(StrictBody):
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


class SignatureEvidence(StrictBody):
    provider: Literal["cosign"]
    verified: bool
    certificateIdentity: str | None = Field(default=None, max_length=500)
    bundleLocation: str | None = Field(default=None, max_length=1000)


class ProvenanceEvidence(StrictBody):
    """What the Sign stage attested with `cosign attest` and then verified (ADR-044)."""

    predicateType: Literal["https://slsa.dev/provenance/v1"]
    verified: bool
    repository: str | None = Field(default=None, max_length=1000)
    commit: str | None = Field(default=None, max_length=64)
    builderId: str | None = Field(default=None, max_length=500)


class RunCiReport(StrictBody):
    autoTest: Literal["passed", "failed", "skipped"]
    coverage: float | None = Field(default=None, ge=0, le=100)
    testsRun: int | None = Field(default=None, ge=0)
    runner: str | None = Field(default=None, max_length=64)


class SecurityEvidenceRequest(StrictBody):
    """Supply-chain evidence a CI run publishes for one immutable artifact."""

    artifactDigest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    artifactRef: str | None = Field(default=None, max_length=1000)
    sbom: SbomEvidence
    vulnerabilityScan: VulnerabilityScanEvidence
    signature: SignatureEvidence
    provenance: ProvenanceEvidence | None = None
    buildRunId: str | None = Field(default=None, max_length=255)
    # What the test stage recorded; copied onto a version registered from this run.
    ciReport: RunCiReport | None = None


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


class SecurityExceptionCreate(StrictBody):
    cve: str = Field(min_length=3, max_length=64)
    artifactDigest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    owner: str = Field(min_length=1, max_length=128)
    reason: str = Field(min_length=1, max_length=1000)
    expiresAt: datetime


class BreakGlassCreate(StrictBody):
    targetType: str = Field(min_length=1, max_length=64)
    targetId: str = Field(min_length=1, max_length=128)
    reason: str = Field(min_length=1, max_length=1000)
    incidentTicket: str = Field(min_length=1, max_length=64)


class BreakGlassApprove(StrictBody):
    ttlMinutes: int = Field(default=60, ge=1, le=240)


class ResourceQuotaUpdate(StrictBody):
    maxConcurrentPipelines: int = Field(default=5, ge=1, le=1000)
    maxConcurrentDeployments: int = Field(default=2, ge=1, le=1000)
    maxProductionRequestsPerDay: int = Field(default=20, ge=1, le=10000)


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

    fields: list[dict[str, str]] = []
    for item in exc.errors():
        cause = item.get("ctx", {}).get("error") if isinstance(item.get("ctx"), dict) else None
        if isinstance(cause, BuildInputError):
            return error(cause.code, cause.message, request.state.correlation_id, 422)
        # Field path and pydantic's message only -- never `input`, which would echo the
        # body (a token pasted into the wrong field included) back into logs and toasts.
        location = ".".join(str(part) for part in item.get("loc", ()) if part != "body")
        fields.append({"field": location or "body", "message": str(item.get("msg", "invalid"))})
    summary = "; ".join(f"{f['field']}: {f['message']}" for f in fields[:5]) or "request validation failed"
    return error("VALIDATION_ERROR", summary, request.state.correlation_id, 422,
                 detail={"fields": fields})


@app.exception_handler(TrafficRoutingUnavailable)
async def traffic_router_unavailable_handler(request: Request, exc: TrafficRoutingUnavailable) -> JSONResponse:
    # 501, not 200 with a stored number: a canary weight that routes nothing is a false green.
    return error("TRAFFIC_ROUTER_NOT_CONFIGURED", str(exc), request.state.correlation_id, 501)


@app.exception_handler(DeliveryError)
async def delivery_exception_handler(request: Request, exc: DeliveryError) -> JSONResponse:
    return error(exc.code, exc.message, request.state.correlation_id, exc.status_code)


@app.exception_handler(PortalError)
async def portal_exception_handler(request: Request, exc: PortalError) -> JSONResponse:
    return error(exc.code, exc.message, request.state.correlation_id, exc.status_code)


@app.get("/livez")
def livez() -> dict[str, object]:
    """Report basic process liveness."""
    return {"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()}


@app.get("/readyz")
def readyz(response: Response) -> dict[str, object]:
    """Report whether the platform is ready to accept and execute work."""
    ready, details = probe_readiness(platform, portal, authenticator)
    if not ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    # Unauthenticated, and exempt from the rate limiter so a load balancer can poll it.
    # A probe failure's own text names what failed -- the Jenkins URL, the DCIM endpoint,
    # a secret file path -- and `/operator/health` is where that belongs.
    return without_operator_detail(details)


@app.get("/operator/health")
def operator_health(response: Response, _: Principal = AdminAccess) -> dict[str, object]:
    """Detailed operator health report with full diagnostics and redacted secrets."""
    ready, details = probe_readiness(platform, portal, authenticator)
    if not ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {
        **details,
        "operatorView": True,
        "version": "0.1.0",
        "environment": os.getenv("NETCI_ENVIRONMENT", "local"),
    }


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


_drift_observation: dict[str, object] = {"at": 0.0, "value": None}


def _controller_drift_observation() -> dict[str, object] | None:
    """The controllers' drift verdict, re-read at most once a minute per replica.

    A JCasC export per controller costs about a second each; a scrape every 15 s must
    not pay that. This is an observation cache for a metric, not durable state: every
    replica publishes its own reading, and the alert rule takes the max.
    """

    probe = getattr(platform.ci_launcher, "controller_drift", None)
    if probe is None:
        return None
    now = time.monotonic()
    if _drift_observation["value"] is None or now - float(_drift_observation["at"]) > _drift_metric_interval_seconds():
        try:
            _drift_observation["value"] = probe()
        except Exception as exc:  # noqa: BLE001 - a failed probe is itself the observation
            _drift_observation["value"] = {"drift": True, "unreachable": {"probe": str(exc)[:200]}, "differing": []}
        _drift_observation["at"] = now
    return _drift_observation["value"]  # type: ignore[return-value]


def _drift_metric_interval_seconds() -> float:
    try:
        return max(5.0, float(os.getenv("NETCI_DRIFT_METRIC_INTERVAL_SECONDS", "60")))
    except ValueError:
        return 60.0


@app.get("/metrics", include_in_schema=False)
def metrics_endpoint() -> PlainResponse:
    """Expose Prometheus formatted metrics."""
    if hasattr(database, "pool_stats"):
        stats = database.pool_stats()
        metrics.gauge_set("netci_database_pool_connections", {"state": "active"}, float(stats.get("active", 0)))
        metrics.gauge_set("netci_database_pool_connections", {"state": "idle"}, float(stats.get("idle", 0)))
    # The same probe /readyz runs, published as gauges: a scrape is how the alerting
    # side learns that Jenkins, Temporal or the router went away.
    ready, details = probe_readiness(platform, portal, authenticator)
    metrics.gauge_set("netci_ready", {}, 1.0 if ready else 0.0)
    for dependency in ("database", "ci", "cd", "dcim", "cosign", "traffic"):
        section = details.get(dependency) or {}
        metrics.gauge_set("netci_dependency_ready", {"dependency": dependency}, 1.0 if section.get("ready") else 0.0)
    metrics.gauge_set("netci_cd_pollers", {}, float((details.get("cd") or {}).get("pollers") or 0))
    metrics.gauge_set("netci_ci_controllers_healthy", {}, float((details.get("ci") or {}).get("healthyControllers") or 0))
    drift = _controller_drift_observation()
    if drift is not None:
        metrics.gauge_set("netci_ci_controllers_drift", {}, 1.0 if drift.get("drift") else 0.0)
        metrics.gauge_set("netci_ci_controllers_unreachable", {}, float(len(drift.get("unreachable") or {})))
    agents = fleet.connections()
    metrics.gauge_set("netci_agents", {"state": "connected"}, float(sum(1 for a in agents if not a["stale"])))
    metrics.gauge_set("netci_agents", {"state": "stale"}, float(sum(1 for a in agents if a["stale"])))
    metrics.gauge_set("netci_replica_info", {"replica": fleet.replica_id}, 1.0)
    return PlainResponse(
        content=metrics.generate_prometheus_text(),
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )


_oidc_discovery: dict[str, object] = {"at": 0.0, "value": None}


def _oidc_browser_config() -> dict[str, object] | None:
    """What the browser needs to start an Authorization Code + PKCE login, or None.

    Server-decided (ADR-015 applies to configuration too): the Portal build carries no
    issuer or client id, so the same bundle serves every installation and a browser
    cannot be pointed at another identity provider by editing a config file. The
    endpoints come from the issuer's discovery document, fetched lazily and kept for
    ten minutes -- identity-provider metadata, not durable state.
    """

    if getattr(authenticator, "mode", "none") != "oidc":
        return None
    client_id = os.getenv("NETCI_OIDC_BROWSER_CLIENT_ID", "").strip()
    issuer = os.getenv("NETCI_OIDC_ISSUER", "").strip().rstrip("/")
    if not client_id or not issuer:
        return None
    now = time.monotonic()
    discovery = _oidc_discovery["value"]
    if discovery is None or now - float(_oidc_discovery["at"]) > 600:
        # Where *this server* reads the discovery document, when that differs from the
        # issuer a browser uses: inside a cluster the public issuer URL can resolve to the
        # pod itself. The document's endpoints are still the identity provider's public
        # ones, and the issuer checked on tokens is still NETCI_OIDC_ISSUER.
        discovery_url = os.getenv("NETCI_OIDC_DISCOVERY_URL", "").strip() or f"{issuer}/.well-known/openid-configuration"
        try:
            with urllib.request.urlopen(discovery_url, timeout=5) as response:
                discovery = json.loads(response.read())
        except (OSError, ValueError) as exc:
            logger.warning("OIDC discovery at %s failed: %s", discovery_url, exc)
            return {"issuer": issuer, "clientId": client_id, "error": "discovery_unavailable"}
        _oidc_discovery.update(at=now, value=discovery)
    assert isinstance(discovery, dict)
    return {
        "issuer": issuer,
        "clientId": client_id,
        "authorizationEndpoint": discovery.get("authorization_endpoint"),
        "tokenEndpoint": discovery.get("token_endpoint"),
        "endSessionEndpoint": discovery.get("end_session_endpoint"),
        "scopes": os.getenv("NETCI_OIDC_BROWSER_SCOPES", "openid profile email").split(),
        "pkce": "S256",
    }


@app.get("/auth/config")
def auth_config() -> dict[str, object]:
    """How a browser signs in: the mode, and for OIDC the public client and endpoints.

    Unauthenticated by necessity -- it is what the login page reads before anyone has
    logged in -- and it reveals nothing secret: a public client has no secret, and the
    issuer's endpoints are published by the issuer itself.
    """

    return {"authMode": getattr(authenticator, "mode", "unknown"), "oidc": _oidc_browser_config()}


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


class CustomStageCreate(StrictBody):
    id: str = Field(pattern=r"^[a-z][a-z0-9-]{1,62}$")
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=1000)
    category: Literal["source", "test", "build", "security", "publish", "deploy", "verify", "custom"] = "custom"
    # A path inside the application's repository, run with bash in the builder after
    # `afterStage`. Never a command: the portal is not a place to type shell.
    script: str = Field(min_length=4, max_length=255)
    afterStage: str = Field(min_length=1, max_length=64)
    # Parameters a module may set for this stage; each reaches the script as an
    # environment variable. Declared here, valued per module in PUT /modules/{id}/stages.
    parameters: list[StageParameterDeclaration] = Field(default_factory=list, max_length=16)


class StageParameterDeclaration(StrictBody):
    name: str = Field(pattern=r"^[A-Z][A-Z0-9_]{0,63}$")
    default: str = Field(default="", max_length=256)
    description: str = Field(default="", max_length=500)


CustomStageCreate.model_rebuild()


@app.post("/stage-catalog", status_code=status.HTTP_201_CREATED)
def register_custom_stage(payload: CustomStageCreate, principal: Principal = AdminAccess) -> dict[str, object]:
    """Propose a custom stage. Platform-admin only, and -- with separation of duties on --
    it runs nowhere until a different administrator approves it: a stage is code on
    every build agent of every module that selects it."""

    return platform.register_custom_stage(
        actor=principal.subject, requires_approval=separation_of_duties_enabled(principal),
        stage_id=payload.id, name=payload.name, description=payload.description,
        category=payload.category, script=payload.script, after_stage=payload.afterStage,
        parameters=[p.model_dump() for p in payload.parameters],
    )


@app.post("/stage-catalog/{stageId}/approve")
def approve_custom_stage(stageId: str, principal: Principal = AdminAccess) -> dict[str, object]:
    return platform.approve_custom_stage(
        stageId, actor=principal.subject, separation_of_duties=separation_of_duties_enabled(principal)
    )


@app.delete("/stage-catalog/{stageId}", status_code=status.HTTP_204_NO_CONTENT)
def remove_custom_stage(stageId: str, principal: Principal = AdminAccess) -> Response:
    platform.remove_custom_stage(stageId, actor=principal.subject)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


class ModuleStagesUpdate(StrictBody):
    stages: list[str] = Field(min_length=1, max_length=64)
    # {"<custom stage id>": {"<PARAM>": "<value>"}}; only declared names, safe values.
    stageParameters: dict[str, dict[str, str]] = Field(default_factory=dict)


@app.get("/modules/{moduleId}/stages")
def get_module_stages(moduleId: str, principal: Principal = ReadAccess) -> dict[str, object]:
    try:
        module = _require_module_access(moduleId, principal)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail={"code": "MODULE_NOT_FOUND", "message": str(exc)}) from exc
    application_id = module.get("applicationId")
    if not application_id:
        raise HTTPException(status_code=409, detail={"code": "MODULE_NOT_PROVISIONED", "message": "module has no delivery application"})
    application = platform.get_application(UUID(str(application_id)))
    return {"moduleId": moduleId, "applicationId": str(application.id), "stages": list(application.stages),
            "stageParameters": dict(application.stage_parameters), "pipelineTemplate": application.pipeline_template}


@app.put("/modules/{moduleId}/stages")
def set_module_stages(moduleId: str, payload: ModuleStagesUpdate, principal: Principal = DeveloperAccess) -> dict[str, object]:
    """Choose which catalog stages this module's pipeline runs.

    Built-ins keep the template's order and the required ones stay; custom stages land
    after their anchor. Takes effect on the next run.
    """

    try:
        module = _require_module_access(moduleId, principal)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail={"code": "MODULE_NOT_FOUND", "message": str(exc)}) from exc
    application_id = module.get("applicationId")
    if not application_id:
        raise HTTPException(status_code=409, detail={"code": "MODULE_NOT_PROVISIONED", "message": "module has no delivery application"})
    application = platform.set_application_stages(
        UUID(str(application_id)), payload.stages, actor=principal.subject, stage_parameters=payload.stageParameters
    )
    return {"moduleId": moduleId, "applicationId": str(application.id), "stages": list(application.stages),
            "stageParameters": dict(application.stage_parameters)}


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


class DcimProvisionRequest(StrictBody):
    systemId: str
    moduleId: str
    runtime: str = "docker"


@app.post("/dcim/auto-provision", status_code=status.HTTP_200_OK)
def auto_provision_dcim_targets(
    payload: DcimProvisionRequest, principal: Principal = AdminAccess
) -> dict[str, object]:
    """Dedicated infrastructure admin endpoint for provisioning targets in DCIM."""
    if not hasattr(portal.dcim_catalog, "auto_provision_targets"):
        raise HTTPException(status_code=400, detail={"code": "DCIM_NOT_NETBOX", "message": "DCIM provider is not NetBox"})
    try:
        devices = portal.dcim_catalog.auto_provision_targets(payload.systemId, payload.moduleId, payload.runtime)
        return {"status": "ok", "provisioned": len(devices), "devices": [d.get("name") for d in devices]}
    except Exception as exc:
        raise HTTPException(status_code=500, detail={"code": "PROVISION_FAILED", "message": str(exc)}) from exc


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
            created_module = portal.attach_module(
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
            return created_module


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


class ModuleOwnerUpdate(StrictBody):
    ownerTeam: str | None = Field(default=None, min_length=1, max_length=255)


@app.put("/modules/{moduleId}/owner")
def set_module_owner(moduleId: str, payload: ModuleOwnerUpdate, principal: Principal = AdminAccess) -> dict[str, object]:
    """Transfer a module to another team. Platform-admin only: a team must not be able
    to hand itself somebody else's module, nor give its own away by mistake."""

    try:
        module = portal.module(moduleId)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail={"code": "MODULE_NOT_FOUND", "message": str(exc)}) from exc
    application_id = module.get("applicationId")
    if not application_id:
        raise HTTPException(status_code=409, detail={"code": "MODULE_NOT_PROVISIONED", "message": "module has no delivery application"})
    owner = (payload.ownerTeam or "").strip() or None
    if owner is None and require_application_owner():
        raise HTTPException(status_code=422, detail={"code": "OWNER_TEAM_REQUIRED", "message": "ownerTeam is required: NETCI_REQUIRE_APPLICATION_OWNER is set"})
    platform.set_application_owner(UUID(str(application_id)), owner, actor=principal.subject)
    return portal.module(moduleId)


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
        active_rev_id = UUID(str(module["activeConfigRevisionId"])) if module.get("activeConfigRevisionId") else None
        run = platform.start_pipeline(
            UUID(str(application_id)),
            commit_sha=payload.commitSha,
            branch=payload.branch,
            environment=payload.environment,
            parameters=(portal.delivery_parameters(moduleId, payload.environment, payload.parameters)
                        if payload.deploy else portal.build_parameters(moduleId, payload.parameters)),
            correlation_id=request.state.correlation_id,
            idempotency_key=idempotency_key,
            started_by=principal.subject,
            config_revision_id=active_rev_id,
            deploy_after_build=payload.deploy,
            trigger={"event": "manual", "sender": principal.subject,
                     "reason": "started from the portal" + ("" if payload.deploy else ", build only")},
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



_GIT_REF_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,200}$")


#: The only URL schemes netCI dials to read a repository. Not file:// -- a module's URL
#: would otherwise name a path on the API's own filesystem.
GIT_URL_SCHEMES = frozenset({"http", "https", "ssh"})
_GIT_ENV = {"GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "/bin/true"}


def list_recent_commits(
    repository_url: str, ref: str, *, limit: int = 15, timeout_seconds: float = 20.0
) -> list[dict[str, str]]:
    """The newest commits on one branch or tag, with subject, author and time.

    The run dialog used to offer only a branch's head SHA, so choosing anything but the
    tip meant pasting hex from somewhere else. This reads the module's own repository --
    the URL registered at onboarding, never one a caller supplies -- with a shallow fetch
    into a throwaway directory: `--depth` commits and no file contents, so asking about a
    large repository does not clone it.
    """

    parsed = urlsplit(repository_url)
    if parsed.scheme not in GIT_URL_SCHEMES:
        raise ValueError(f"unsupported repository URL scheme {parsed.scheme!r}")
    # The ref reaches git as an argument; a leading '-' would be read as an option.
    if ref.startswith("-") or not _GIT_REF_NAME.fullmatch(ref):
        raise ValueError(f"{ref!r} is not a branch or tag name")
    limit = max(1, min(int(limit), 50))
    env = {**os.environ, **_GIT_ENV}
    with tempfile.TemporaryDirectory(prefix="netci-commits-") as scratch:
        subprocess.run(["git", "init", "--bare", "-q", scratch], check=True, env=env,
                       capture_output=True, timeout=timeout_seconds)
        fetch = subprocess.run(
            ["git", "-C", scratch, "fetch", "--quiet", "--no-tags", f"--depth={limit}",
             "--filter=blob:none", repository_url, ref],
            capture_output=True, text=True, timeout=timeout_seconds, env=env, check=False,
        )
        if fetch.returncode != 0:
            raise RuntimeError((fetch.stderr or fetch.stdout).strip()[-300:] or "git fetch failed")
        log = subprocess.run(
            ["git", "-C", scratch, "log", f"-n{limit}", "--format=%H%x1f%s%x1f%an%x1f%aI", "FETCH_HEAD"],
            capture_output=True, text=True, timeout=timeout_seconds, env=env, check=False,
        )
        if log.returncode != 0:
            raise RuntimeError((log.stderr or log.stdout).strip()[-300:] or "git log failed")
    commits: list[dict[str, str]] = []
    for line in log.stdout.splitlines():
        parts = line.split("\x1f")
        if len(parts) == 4 and re.fullmatch(r"[0-9a-f]{40}", parts[0]):
            sha, subject, author, when = parts
            commits.append({"sha": sha, "subject": subject[:200], "author": author[:120], "committedAt": when})
    return commits


def list_remote_refs(repository_url: str, timeout_seconds: float = 15.0) -> dict[str, list[dict[str, str]]]:
    """Branches and tags of a repository with the commit each points at, from the
    repository itself (`git ls-remote`) -- what the "run this branch" dialog offers.

    Read-only and anonymous: repositories that need credentials answer with an
    error the caller sees as "refs unavailable", never with a guess. Only http(s)
    and ssh URLs are dialled, and git's own credential prompting is disabled so a
    protected repository fails fast instead of hanging.
    """

    parsed = urlsplit(repository_url)
    if parsed.scheme not in GIT_URL_SCHEMES:
        raise ValueError(f"unsupported repository URL scheme {parsed.scheme!r}")
    result = subprocess.run(
        ["git", "ls-remote", "--heads", "--tags", "--refs", repository_url],
        capture_output=True, text=True, timeout=timeout_seconds, check=False,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "/bin/true"},
    )
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout).strip()[-300:] or "git ls-remote failed")
    branches: list[dict[str, str]] = []
    tags: list[dict[str, str]] = []
    for line in result.stdout.splitlines():
        sha, _, ref = line.partition("\t")
        if not re.fullmatch(r"[0-9a-f]{40}", sha):
            continue
        if ref.startswith("refs/heads/"):
            name = ref[len("refs/heads/"):]
            if _GIT_REF_NAME.fullmatch(name):
                branches.append({"name": name, "sha": sha})
        elif ref.startswith("refs/tags/"):
            name = ref[len("refs/tags/"):]
            if _GIT_REF_NAME.fullmatch(name):
                tags.append({"name": name, "sha": sha})
    branches.sort(key=lambda item: (item["name"] not in {"main", "master"}, item["name"]))
    tags.sort(key=lambda item: item["name"], reverse=True)
    return {"branches": branches, "tags": tags}


@app.get("/modules/{moduleId}/git-refs")
def get_module_git_refs(moduleId: str, principal: Principal = ReadAccess) -> dict[str, object]:
    """What the module's repository has to build: branches and tags with their commits."""

    try:
        module = portal.module(moduleId)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail={"code": "MODULE_NOT_FOUND", "message": str(exc)}) from exc
    _require_module_access(moduleId, principal)
    repository_url = str(module.get("repositoryUrl") or "")
    try:
        refs = list_remote_refs(repository_url)
    except (ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
        return {"moduleId": moduleId, "repositoryUrl": repository_url, "branches": [], "tags": [], "error": str(exc)[:300]}
    return {"moduleId": moduleId, "repositoryUrl": repository_url, **refs, "error": None}


@app.get("/modules/{moduleId}/git-commits")
def get_module_git_commits(
    moduleId: str,
    ref: str = Query(default="main", min_length=1, max_length=255),
    limit: int = Query(default=15, ge=1, le=50),
    principal: Principal = ReadAccess,
) -> dict[str, object]:
    """Recent commits on a branch or tag of the module's repository, for the run dialog."""

    try:
        module = portal.module(moduleId)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail={"code": "MODULE_NOT_FOUND", "message": str(exc)}) from exc
    _require_module_access(moduleId, principal)
    repository_url = str(module.get("repositoryUrl") or "")
    try:
        items = list_recent_commits(repository_url, ref, limit=limit)
    except ValueError as exc:
        # A malformed ref is the caller's mistake; say so rather than report an empty list.
        raise HTTPException(status_code=422, detail={"code": "INVALID_GIT_REF", "message": str(exc)}) from exc
    except (RuntimeError, subprocess.TimeoutExpired, subprocess.CalledProcessError) as exc:
        # The repository could not be read: an honest error, never an invented history.
        return {"moduleId": moduleId, "ref": ref, "items": [], "error": str(exc)[:300]}
    return {"moduleId": moduleId, "ref": ref, "items": items, "error": None}


@app.get("/git/info")
def get_git_info(principal: Principal = ReadAccess) -> dict[str, object]:
    """Which commit this process was built from, or an honest "unknown".

    This used to default to a hard-coded SHA and swallow every error -- including the
    NameError from `subprocess` never having been imported -- so it always answered
    with a stale commit while looking like it had worked. A build identity the server
    cannot establish is reported as `null`, never as a guess.
    """

    commit_sha: str | None = os.getenv("NETCI_BUILD_COMMIT", "").strip() or None
    branch: str | None = os.getenv("NETCI_BUILD_BRANCH", "").strip() or None
    source = "environment" if commit_sha else None
    if commit_sha is None:
        try:
            res = subprocess.run(
                ["git", "rev-parse", "HEAD"], capture_output=True, text=True, timeout=2
            )
            if res.returncode == 0 and res.stdout.strip():
                commit_sha = res.stdout.strip()
                source = "git"
            b_res = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                capture_output=True, text=True, timeout=2,
            )
            if b_res.returncode == 0 and b_res.stdout.strip():
                branch = b_res.stdout.strip()
        except (OSError, subprocess.SubprocessError) as exc:
            logger.warning("build identity unavailable: %s", exc)

    return {
        "currentCommitSha": commit_sha,
        "currentBranch": branch,
        "source": source,
    }


def _discover_sample_apps() -> list[dict[str, object]]:
    """The sample applications this checkout actually ships, read from disk.

    This used to be a hard-coded list naming repositories under `github.com/netci/`
    that do not exist, with descriptions like "zero-CVE Trivy baseline" nobody had
    measured. A wizard that offers a repository Jenkins cannot clone is a demo, not an
    onboarding path. The runtime is inferred from what is really in each directory, and
    the repository URL comes from configuration -- absent, it is `null`, and the UI says
    so rather than inventing one.
    """

    root = Path(__file__).resolve().parents[2] / "sample-apps"
    base = os.getenv("NETCI_SAMPLE_APPS_REPOSITORY_BASE", "").strip().rstrip("/") or None
    found: list[dict[str, object]] = []
    if not root.is_dir():
        return found
    for entry in sorted(root.iterdir()):
        if not entry.is_dir() or entry.name.startswith((".", "_")) or entry.name == "base-python":
            continue
        files = {item.name for item in entry.iterdir()}
        # The Kubernetes sample keeps its chart under deploy/helm/, the same place the
        # e2e-kubernetes gate deploys it from; the runtime is what the gate proves, not
        # what the directory name suggests.
        chart = root.parent / "deploy" / "helm" / f"sample-{entry.name.removeprefix('hello-')}-app"
        if (chart / "Chart.yaml").is_file() or "Chart.yaml" in files:
            runtime, template = "kubernetes", "kubernetes-ci-cd-v1"
        elif "go.mod" in files and "Dockerfile" not in files:
            runtime, template = "systemd", "systemd-ansible-ci-cd-v1"
        elif "Dockerfile" in files:
            runtime, template = "docker", "container-ci-cd-v1"
        else:
            continue
        git_url = os.getenv("NETCI_GIT_URL", "").strip()
        repo_url = git_url or (f"{base}/netci.git" if base else None)
        found.append(
            {
                "id": entry.name,
                "name": entry.name.replace("-", " ").title(),
                "runtime": runtime,
                "pipelineTemplate": template,
                "path": f"sample-apps/{entry.name}",
                "repositoryUrl": repo_url,
                "hasTests": any(name.startswith("test_") or name.endswith("_test.go") for name in files),
            }
        )
    return found


@app.get("/sample-apps")
def list_sample_apps(principal: Principal = ReadAccess) -> dict[str, object]:
    """Sample applications available for quick-start onboarding, discovered from disk."""

    items = _discover_sample_apps()
    return {
        "repositoryBaseConfigured": bool(os.getenv("NETCI_SAMPLE_APPS_REPOSITORY_BASE", "").strip()),
        "items": items,
    }


class PromotionCreate(StrictBody):
    #: The run whose artifact moves. Its digest is what was tested; nothing is rebuilt.
    pipelineRunId: UUID
    environment: Environment


@app.get("/modules/{moduleId}/delivery-rules")
def get_module_delivery_rules(moduleId: str, principal: Principal = ReadAccess) -> dict[str, object]:
    """What an SCM event will cause for this module, and what a promotion must prove."""

    try:
        _require_module_access(moduleId, principal)
        return {"moduleId": moduleId, **portal.delivery_rules(moduleId).as_json()}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail={"code": "MODULE_NOT_FOUND", "message": str(exc)}) from exc


@app.post("/modules/{moduleId}/promotions", status_code=status.HTTP_202_ACCEPTED)
def promote_module_artifact(
    moduleId: str,
    payload: PromotionCreate,
    principal: Principal = DeveloperAccess,
) -> dict[str, object]:
    _require_environment_role(payload.environment, principal)
    try:
        _require_module_access(moduleId, principal)
        return portal.promote(
            moduleId, pipeline_run_id=payload.pipelineRunId, environment=payload.environment,
            actor=principal.subject,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail={"code": "MODULE_NOT_FOUND", "message": str(exc)}) from exc


@app.get("/modules/{moduleId}/versions")
def list_module_versions(moduleId: str, principal: Principal = ReadAccess) -> dict[str, object]:
    try:
        _require_module_access(moduleId, principal)
        return portal.versions(moduleId)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail={"code": "MODULE_NOT_FOUND", "message": str(exc)}) from exc


# ------------------------------------------------------------- versioned config revisions & DCIM

@app.get("/modules/{moduleId}/config-revisions")
def list_module_config_revisions(moduleId: str, principal: Principal = ReadAccess) -> dict[str, object]:
    try:
        _require_module_access(moduleId, principal)
        return portal.config_revisions(moduleId)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail={"code": "MODULE_NOT_FOUND", "message": str(exc)}) from exc


@app.post("/modules/{moduleId}/config-revisions", status_code=status.HTTP_201_CREATED)
def propose_module_config_revision(
    moduleId: str,
    payload: ConfigRevisionCreate,
    principal: Principal = DeveloperAccess,
) -> dict[str, object]:
    try:
        _require_module_access(moduleId, principal)
        return portal.propose_config_revision(
            moduleId,
            pipeline_config=payload.pipelineConfig,
            deployment_config=[
                item.model_dump(mode="json", exclude_none=True) for item in payload.deploymentConfig
            ],
            change_summary=payload.changeSummary,
            expected_version=payload.expectedVersion,
            actor=principal.subject,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail={"code": "MODULE_NOT_FOUND", "message": str(exc)}) from exc
    except PortalError as exc:
        raise HTTPException(status_code=exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc


@app.get("/modules/{moduleId}/config-revisions/diff")
def diff_module_config_revisions(
    moduleId: str,
    fromRev: int,
    toRev: int,
    principal: Principal = ReadAccess,
) -> dict[str, object]:
    try:
        _require_module_access(moduleId, principal)
        return portal.diff_config_revisions(moduleId, fromRev, toRev)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail={"code": "REVISION_NOT_FOUND", "message": str(exc)}) from exc


@app.post("/modules/{moduleId}/config-revisions/{revisionId}/approve")
def approve_module_config_revision(
    moduleId: str,
    revisionId: UUID,
    principal: Principal = ReviewerAccess,
) -> dict[str, object]:
    try:
        _require_module_access(moduleId, principal)
        return portal.approve_config_revision(moduleId, revisionId, actor=principal.subject)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail={"code": "REVISION_NOT_FOUND", "message": str(exc)}) from exc
    except PortalError as exc:
        raise HTTPException(status_code=exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc


@app.post("/modules/{moduleId}/config-revisions/{revisionId}/reject")
def reject_module_config_revision(
    moduleId: str,
    revisionId: UUID,
    payload: ConfigRevisionReject,
    principal: Principal = ReviewerAccess,
) -> dict[str, object]:
    try:
        _require_module_access(moduleId, principal)
        return portal.reject_config_revision(moduleId, revisionId, actor=principal.subject, reason=payload.reason)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail={"code": "REVISION_NOT_FOUND", "message": str(exc)}) from exc
    except PortalError as exc:
        raise HTTPException(status_code=exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc


@app.post("/modules/{moduleId}/config-revisions/{revisionNumber}/rollback")
def rollback_module_config_revision(
    moduleId: str,
    revisionNumber: int,
    principal: Principal = DeveloperAccess,
) -> dict[str, object]:
    try:
        _require_module_access(moduleId, principal)
        return portal.rollback_config_revision(moduleId, revisionNumber, actor=principal.subject)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail={"code": "REVISION_NOT_FOUND", "message": str(exc)}) from exc
    except PortalError as exc:
        raise HTTPException(status_code=exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc


@app.post("/modules/{moduleId}/config/apply")
def apply_module_config(
    moduleId: str,
    payload: ConfigApplyRequest,
    principal: Principal = DeveloperAccess,
) -> dict[str, object]:
    try:
        _require_module_access(moduleId, principal)
        return portal.apply_config_revision(
            moduleId,
            environment=payload.environment,
            revision_id=payload.revisionId,
            fencing_token=payload.fencingToken,
            actor=principal.subject,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": str(exc)}) from exc
    except PortalError as exc:
        raise HTTPException(status_code=exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc


@app.get("/modules/{moduleId}/drift")
def detect_module_drift(moduleId: str, principal: Principal = ReadAccess) -> dict[str, object]:
    try:
        _require_module_access(moduleId, principal)
        return portal.detect_drift(moduleId)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail={"code": "MODULE_NOT_FOUND", "message": str(exc)}) from exc


@app.get("/servers/health")
def list_servers_health(serverName: str | None = None, _: Principal = ReadAccess) -> dict[str, object]:
    with database.transaction() as session:
        if serverName:
            rec = session.server_health(serverName)
            items = [rec] if rec else []
        else:
            items = list(session.list_server_health())
    return {
        "count": len(items),
        "items": [
            {
                "serverName": r.server_name,
                "status": r.status,
                "source": r.source,
                "freshnessSeconds": r.freshness_seconds,
                "details": r.details,
                "observedAt": r.observed_at.isoformat(),
            }
            for r in items
        ],
    }


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
            git_tag_url=str(payload.gitTagUrl) if payload.gitTagUrl else "",
            artifact_url=str(payload.artifactUrl) if payload.artifactUrl else "",
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
            strategy=payload.strategy,
            strategy_config=dict(payload.strategyConfig, canary_rules=payload.canaryRules) if payload.canaryRules else payload.strategyConfig,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail={"code": "MODULE_NOT_FOUND", "message": str(exc)}) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail={"code": "VERSION_NOT_AVAILABLE", "message": str(exc)}) from exc
    except PortalError as exc:
        raise HTTPException(status_code=exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc


@app.get("/production-requests/{requestId}/plan")
def get_production_request_plan(requestId: str, principal: Principal = ReadAccess) -> dict[str, object]:
    existing = portal.production_request(requestId)
    if existing is None:
        raise HTTPException(
            status_code=404, detail={"code": "REQUEST_NOT_FOUND", "message": "production request not found"}
        )
    return {
        "id": requestId,
        "requestId": requestId,
        "status": existing.get("status"),
        "strategy": existing.get("strategy"),
        "releasePlan": existing.get("releasePlan"),
        "modules": existing.get("modules"),
        "scheduledFor": existing.get("scheduledFor"),
        "comment": existing.get("comment"),
        "rollbackStrategy": existing.get("rollbackStrategy"),
        "deploymentId": existing.get("deploymentId"),
        "canaryRules": existing.get("canaryRules") or existing.get("strategyConfig", {}).get("canary_rules", {}),
    }


class AdvanceCanaryRequest(StrictBody):
    #: Refused when non-empty (ADR-046): netCI reads the canary's metrics itself. Declared
    #: so a client that still sends them gets a 422 naming why, not a silent drop.
    metrics: dict[str, float] = Field(default_factory=dict)
    canaryRules: dict[str, Any] = Field(default_factory=dict)
    #: When netCI cannot analyse the canary (no queries, no Prometheus, no data), a
    #: reviewer may still advance it -- recorded as an advance WITHOUT analysis.
    overrideReason: str | None = Field(default=None, min_length=10, max_length=500)


def _canary_analysis(request: dict[str, Any], deployment_id: str) -> tuple[VerificationResult | None, str]:
    """The canary verdict from metrics netCI reads, or why there is none."""

    modules = request.get("modules") or []
    module = next((m for m in modules if str(m.get("deploymentId") or "") == deployment_id), modules[0] if modules else None)
    if not module:
        return None, "the production request names no module"
    module_id = str(module.get("moduleId"))
    spec = portal.verification_spec(module_id)
    if spec is None:
        return None, f"module {module_id} declares no verification queries (pipelineConfig.verification)"
    values: dict[str, float | None] = {}
    try:
        for name, template in spec.queries.items():
            values[name] = metrics_source.query(
                render_query(template, release=module_id, environment="prod", track="canary")
            )
    except (MetricsUnavailable, VerificationConfigError) as exc:
        return None, f"canary metrics could not be read: {exc}"
    missing = [name for name, value in values.items() if value is None]
    if missing:
        # "Cannot tell" is not "bad": no abort, but no advance on it either.
        return None, f"Prometheus returned no data for the canary ({', '.join(missing)})"
    return verdict_for_canary(spec, values.get("errorRate"), values.get("p95LatencyMs")), ""


@app.post("/production-requests/{requestId}/canary/advance")
def advance_canary_step(
    requestId: str,
    payload: AdvanceCanaryRequest | None = None,
    principal: Principal = ReviewerAccess,
) -> dict[str, object]:
    existing = portal.production_request(requestId)
    if existing is None:
        raise HTTPException(
            status_code=404, detail={"code": "REQUEST_NOT_FOUND", "message": "production request not found"}
        )
    deployment_id = existing.get("deploymentId")
    if not deployment_id:
        raise HTTPException(
            status_code=400, detail={"code": "NO_ACTIVE_DEPLOYMENT", "message": "no deployment currently active for this request"}
        )
    if payload and payload.metrics:
        raise HTTPException(
            status_code=422,
            detail={"code": "CANARY_METRICS_ARE_READ_BY_NETCI",
                    "message": "netCI reads canary metrics from Prometheus itself; metrics in the request are not accepted"},
        )
    analysis, unavailable = _canary_analysis(existing, str(deployment_id))
    override = (payload.overrideReason or "").strip() if payload else ""
    if analysis is None and not override:
        raise HTTPException(
            status_code=409,
            detail={"code": "CANARY_ANALYSIS_UNAVAILABLE",
                    "message": f"{unavailable}; to advance anyway give an overrideReason -- it is recorded as an advance without analysis"},
        )
    if payload and payload.canaryRules:
        modules = existing.get("modules") or []
        for m in modules:
            mod_id = m.get("moduleId")
            if mod_id:
                # These rules are what splits production traffic. Swallowing a failure
                # here used to return 202 while the split was never applied, so the
                # canary "advanced" with every request still going to stable -- a green
                # answer about a state that was never established. Refuse instead, and
                # name the module, so the operator knows which one to look at.
                try:
                    mod = portal.module(mod_id)
                    app_id = mod.get("applicationId")
                    if app_id:
                        default_traffic_router.set_canary_rules(str(app_id), "prod", payload.canaryRules)
                except TrafficRoutingUnavailable:
                    raise
                except HTTPException:
                    raise
                except Exception as exc:
                    raise HTTPException(
                        status_code=502,
                        detail={
                            "code": "CANARY_RULES_NOT_APPLIED",
                            "message": f"canary traffic rules could not be applied for module {mod_id}: {type(exc).__name__}",
                        },
                    ) from exc
    coordinator = ReleasePlanCoordinator(portal, platform)
    try:
        return coordinator.advance_canary(requestId, UUID(str(deployment_id)), analysis=analysis,
                                          override_reason=override or None, actor=principal.subject)
    except TrafficRoutingUnavailable:
        raise
    except KeyError as exc:
        raise HTTPException(
            status_code=404, detail={"code": "CANARY_TARGET_NOT_FOUND", "message": str(exc)}
        ) from exc
    except ValueError as exc:
        # The coordinator raises ValueError for a request the caller really did get
        # wrong: no release plan, an unverified version, a runtime that cannot take a
        # canary. Those are 400s and their messages are written for the caller.
        raise HTTPException(
            status_code=400, detail={"code": "CANARY_ERROR", "message": str(exc)}
        ) from exc
    except Exception as exc:
        # Anything else is ours. Answering 400 told the operator their request was wrong
        # when the fault was here, and str(exc) on a database error carries SQL and table
        # names out to the caller. The detail belongs in the log, not the response.
        logger.exception("canary operation failed for production request %s", requestId)
        raise HTTPException(
            status_code=500,
            detail={"code": "CANARY_INTERNAL_ERROR",
                    "message": "the canary operation failed inside netCI; see the server log "
                               f"with correlation to request {requestId}"},
        ) from exc


class AbortCanaryRequest(StrictBody):
    reason: str = Field(default="Aborted by operator", min_length=1, max_length=500)


@app.post("/production-requests/{requestId}/canary/abort")
def abort_canary_step(
    requestId: str,
    payload: AbortCanaryRequest | None = None,
    principal: Principal = ReviewerAccess,
) -> dict[str, object]:
    existing = portal.production_request(requestId)
    if existing is None:
        raise HTTPException(
            status_code=404, detail={"code": "REQUEST_NOT_FOUND", "message": "production request not found"}
        )
    deployment_id = existing.get("deploymentId")
    if not deployment_id:
        raise HTTPException(
            status_code=400, detail={"code": "NO_ACTIVE_DEPLOYMENT", "message": "no deployment currently active for this request"}
        )
    reason = payload.reason if payload else "Aborted by operator"
    coordinator = ReleasePlanCoordinator(portal, platform)
    try:
        return coordinator.abort_canary(requestId, UUID(str(deployment_id)), reason)
    except TrafficRoutingUnavailable:
        raise
    except KeyError as exc:
        raise HTTPException(
            status_code=404, detail={"code": "CANARY_TARGET_NOT_FOUND", "message": str(exc)}
        ) from exc
    except ValueError as exc:
        # The coordinator raises ValueError for a request the caller really did get
        # wrong: no release plan, an unverified version, a runtime that cannot take a
        # canary. Those are 400s and their messages are written for the caller.
        raise HTTPException(
            status_code=400, detail={"code": "CANARY_ERROR", "message": str(exc)}
        ) from exc
    except Exception as exc:
        # Anything else is ours. Answering 400 told the operator their request was wrong
        # when the fault was here, and str(exc) on a database error carries SQL and table
        # names out to the caller. The detail belongs in the log, not the response.
        logger.exception("canary operation failed for production request %s", requestId)
        raise HTTPException(
            status_code=500,
            detail={"code": "CANARY_INTERNAL_ERROR",
                    "message": "the canary operation failed inside netCI; see the server log "
                               f"with correlation to request {requestId}"},
        ) from exc


@app.get("/deployments/{deploymentId}/traffic")
def get_deployment_traffic(deploymentId: UUID, principal: Principal = ReadAccess) -> dict[str, object]:
    deployment = platform.get_deployment(deploymentId)
    if deployment is None:
        raise HTTPException(status_code=404, detail={"code": "DEPLOYMENT_NOT_FOUND", "message": "deployment not found"})
    status_info = default_traffic_router.get_routing_status(str(deployment.application_id), deployment.environment.value)
    return {
        "deploymentId": str(deploymentId),
        "applicationId": str(deployment.application_id),
        "environment": deployment.environment.value,
        "strategy": deployment.strategy,
        "trafficWeight": deployment.traffic_weight,
        "activeColor": deployment.active_color,
        "canaryStep": deployment.canary_step,
        "routerStatus": status_info,
    }


class TrafficSwitchRequest(StrictBody):
    activeColor: Literal["blue", "green"]


@app.post("/deployments/{deploymentId}/traffic/switch")
def switch_deployment_traffic(
    deploymentId: UUID, payload: TrafficSwitchRequest, principal: Principal = ReviewerAccess
) -> dict[str, object]:
    """Point the stable ingress at a colour (ADR-035): the blue/green switch-back.

    Both colours keep running after a blue/green release, so going back is one patch,
    not a redeploy. The router refuses a colour with no ready endpoints. Recorded on the
    deployment (`activeColor`) and in the audit log.
    """

    deployment = platform.get_deployment(deploymentId)
    if deployment is None:
        raise HTTPException(status_code=404, detail={"code": "DEPLOYMENT_NOT_FOUND", "message": "deployment not found"})
    _require_application_access(platform.get_application(deployment.application_id), principal)
    if deployment.strategy != "blue_green":
        raise HTTPException(status_code=409, detail={"code": "NOT_BLUE_GREEN", "message": f"deployment strategy is {deployment.strategy}; only blue/green deployments switch colours"})
    try:
        status_info = default_traffic_router.switch_route(str(deployment.application_id), deployment.environment.value, payload.activeColor)
    except TrafficRoutingUnavailable:
        raise
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=409, detail={"code": "TRAFFIC_SWITCH_REFUSED", "message": str(exc)}) from exc
    with database.transaction() as session:
        session.update_deployment_traffic(deploymentId, traffic_weight=deployment.traffic_weight, active_color=payload.activeColor)
        session.apply(UnitOfWork(audit=[AuditRecord(
            "deployment.traffic_switched", application_id=deployment.application_id, deployment_id=deploymentId,
            actor=principal.subject, payload={"activeColor": payload.activeColor, "previous": deployment.active_color, "routerStatus": status_info},
        )]))
    return {"deploymentId": str(deploymentId), "activeColor": payload.activeColor, "previousColor": deployment.active_color, "routerStatus": status_info}


@app.post("/production-requests/{requestId}/approve", status_code=status.HTTP_202_ACCEPTED)
def approve_production_request(
    requestId: str, payload: PortalApprovalRequest, principal: Principal = ReviewerAccess
) -> dict[str, object]:
    existing = portal.production_request(requestId)
    if existing is None:
        raise HTTPException(
            status_code=404, detail={"code": "REQUEST_NOT_FOUND", "message": "production request not found"}
        )
    target_apps: list[Application] = []
    for requested_module in existing.get("modules") or []:
        module = portal.module(str(requested_module["moduleId"]))
        application_id = module.get("applicationId")
        if application_id:
            app_obj = platform.get_application(UUID(str(application_id)))
            _require_application_access(app_obj, principal)
            target_apps.append(app_obj)

    # Evaluate Policy Engine with durable auditing
    with database.transaction() as session:
        first_app = target_apps[0] if target_apps else Application(
            name="default",
            repository_url="https://github.com/example/repo",
            pipeline_template="container-ci-cd-v1",
            runtime=Runtime.DOCKER,
            default_environment=Environment.PROD,
            stages=(),
            owner_team=None,
            id=uuid4(),
            created_at=datetime.now(timezone.utc),
        )
        decision = PolicyEngine.evaluate_and_record_production_approval(
            session,
            request_id=requestId,
            application=first_app,
            requested_by=str(existing.get("requestedBy") or ""),
            approver=principal.subject,
            approver_roles=principal.roles,
            module_count=len(existing.get("modules") or []),
            run_automation_tests=bool(existing.get("runAutomationTests", True)),
            rollback_strategy=str(existing.get("rollbackStrategy") or "automatic"),
        )
        if not decision.allowed and separation_of_duties_enabled(principal):
            sod_status = decision.checks.get("separation_of_duties")
            if sod_status and sod_status != "pass":
                raise HTTPException(
                    status_code=403, detail={"code": "SEPARATION_OF_DUTIES", "message": decision.reason}
                )
            raise HTTPException(
                status_code=403, detail={"code": "POLICY_DENIED", "message": decision.reason}
            )

    try:
        res = portal.approve_request(requestId, principal.subject, payload.comment)
        res["policyDecision"] = {
            "id": str(decision.id),
            "allowed": decision.allowed,
            "riskScore": decision.risk_score,
            "reason": decision.reason,
            "checks": decision.checks,
        }
        return res
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
def list_audit_events(
    systemId: str | None = None,
    moduleId: str | None = None,
    limit: int | None = None,
    cursor: str | None = None,
    principal: Principal = ReadAccess,
) -> Any:
    if cursor is not None or limit is not None:
        bounded_limit = max(1, min(limit or 50, 100))
        with database.transaction() as session:
            records, next_cursor, has_more = session.audit_records_paginated(
                limit=bounded_limit, cursor=cursor
            )
        return {
            "items": [
                {
                    "id": str(r.id),
                    "eventType": r.event_type,
                    "applicationId": str(r.application_id) if r.application_id else None,
                    "pipelineRunId": str(r.pipeline_run_id) if r.pipeline_run_id else None,
                    "deploymentId": str(r.deployment_id) if r.deployment_id else None,
                    "actor": r.actor,
                    "correlationId": r.correlation_id,
                    "payload": r.payload,
                    "occurredAt": r.occurred_at.isoformat(),
                }
                for r in records
            ],
            "nextCursor": next_cursor,
            "hasMore": has_more,
        }
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


# ------------------------------------------------------------- notifications & outbox

@app.get("/notifications")
def list_notifications(
    status: str | None = None,
    limit: int = 50,
    cursor: str | None = None,
    principal: Principal = ReadAccess,
) -> dict[str, Any]:
    stat_enum = None
    if status:
        try:
            stat_enum = NotificationStatus(status)
        except ValueError:
            raise HTTPException(
                status_code=422,
                detail={"code": "INVALID_NOTIFICATION_STATUS", "message": f"invalid notification status: {status}"},
            )
    bounded_limit = max(1, min(limit, 100))
    with database.transaction() as session:
        items, next_cursor, has_more = session.notifications_paginated(
            status=stat_enum, limit=bounded_limit, cursor=cursor
        )
    return {
        "items": [
            {
                "id": str(n.id),
                "eventType": n.event_type,
                "aggregateType": n.aggregate_type,
                "aggregateId": n.aggregate_id,
                "payload": n.payload,
                "recipient": n.recipient,
                "status": n.status.value,
                "attempt": n.attempt,
                "maxAttempts": n.max_attempts,
                "lastAttemptAt": n.last_attempt_at.isoformat() if n.last_attempt_at else None,
                "nextAttemptAt": n.next_attempt_at.isoformat() if n.next_attempt_at else None,
                "lastError": n.last_error,
                "createdAt": n.created_at.isoformat(),
                "deliveredAt": n.delivered_at.isoformat() if n.delivered_at else None,
            }
            for n in items
        ],
        "nextCursor": next_cursor,
        "hasMore": has_more,
    }


@app.post("/notifications/{notificationId}/retry")
def retry_notification(
    notificationId: UUID,
    principal: Principal = AdminAccess,
) -> dict[str, Any]:
    with database.transaction() as session:
        n = session.notification(notificationId)
        if not n:
            raise HTTPException(
                status_code=404,
                detail={"code": "NOTIFICATION_NOT_FOUND", "message": "notification not found"},
            )
        updated = session.update_notification_status(
            notificationId,
            status=NotificationStatus.PENDING,
            attempt=0,
            next_attempt_at=datetime.now(timezone.utc),
            last_error=None,
        )
    assert updated is not None
    return {
        "id": str(updated.id),
        "status": updated.status.value,
        "nextAttemptAt": updated.next_attempt_at.isoformat(),
    }


# ------------------------------------------------------------- cursor pagination lists

@app.get("/pipeline-runs")
def list_pipeline_runs(
    applicationId: UUID | None = None,
    limit: int = 50,
    cursor: str | None = None,
    principal: Principal = ReadAccess,
) -> dict[str, Any]:
    bounded_limit = max(1, min(limit, 100))
    with database.transaction() as session:
        runs, next_cursor, has_more = session.pipeline_runs_paginated(
            application_id=applicationId, limit=bounded_limit, cursor=cursor
        )
    return {
        "items": [pipeline_json(r) for r in runs],
        "nextCursor": next_cursor,
        "hasMore": has_more,
    }


@app.get("/deployments")
def list_deployments(
    applicationId: UUID | None = None,
    limit: int = 50,
    cursor: str | None = None,
    principal: Principal = ReadAccess,
) -> dict[str, Any]:
    bounded_limit = max(1, min(limit, 100))
    with database.transaction() as session:
        deps, next_cursor, has_more = session.deployments_paginated(
            application_id=applicationId, limit=bounded_limit, cursor=cursor
        )
    return {
        "items": [deployment_json(d) for d in deps],
        "nextCursor": next_cursor,
        "hasMore": has_more,
    }


# ------------------------------------------------------------- retention

@app.post("/admin/retention/purge")
def trigger_retention_purge(
    principal: Principal = AdminAccess,
) -> dict[str, Any]:
    outcome = run_retention_pass()
    if outcome is None:
        raise HTTPException(status_code=409, detail={"code": "RETENTION_PASS_IN_PROGRESS", "message": "another replica is running the retention pass"})
    return outcome



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


@app.post("/applications/{applicationId}/scm", status_code=status.HTTP_201_CREATED)
def configure_application_scm(
    applicationId: UUID,
    payload: ScmIntegrationCreate,
    principal: Principal = DeveloperAccess,
) -> dict[str, object]:
    """Configure or update SCM webhook & repository identity for an application."""
    app = platform.get_application(applicationId)
    _require_application_access(app, principal)

    secret_hash = hashlib.sha256(payload.secretToken.encode("utf-8")).hexdigest()
    integration = ScmIntegration(
        application_id=applicationId,
        provider=payload.provider,
        repository_identity=payload.repositoryIdentity.strip(),
        secret_token=payload.secretToken,
        secret_token_hash=secret_hash,
        credential_reference=payload.credentialReference.strip() if payload.credentialReference else None,
        enabled=payload.enabled,
    )
    with database.transaction() as session:
        existing = session.scm_integration_for_repository(payload.provider, integration.repository_identity)
        if existing and existing.application_id != applicationId:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"repository {integration.repository_identity} is already configured for another application",
            )
        session.upsert_scm_integration(integration)
        saved = session.scm_integration_for_application(applicationId, payload.provider)
        assert saved is not None

    return scm_integration_json(saved)


@app.get("/applications/{applicationId}/scm")
def get_application_scm(
    applicationId: UUID,
    provider: ScmProviderType | None = None,
    principal: Principal = ReadAccess,
) -> dict[str, object]:
    """Get active SCM integration for an application (secrets are redacted)."""
    app = platform.get_application(applicationId)
    _require_application_access(app, principal)
    with database.transaction() as session:
        integration = session.scm_integration_for_application(applicationId, provider)
        if integration is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no SCM integration found for application")
        return scm_integration_json(integration)


@app.post("/webhooks/scm/{provider}", status_code=status.HTTP_200_OK)
async def receive_scm_webhook(
    provider: str,
    request: Request,
) -> JSONResponse:
    """Receive, verify, deduplicate, and process external SCM webhooks."""
    raw_body = await request.body()
    if len(raw_body) > MAX_WEBHOOK_PAYLOAD_BYTES:
        raise HTTPException(
            status.HTTP_413_CONTENT_TOO_LARGE,
            "webhook payload exceeds maximum permitted size of 1MB",
        )

    try:
        provider_type = ScmProviderType(provider.lower())
        scm_provider = get_scm_provider(provider_type)
    except ValueError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"unsupported SCM provider: {provider}")

    headers = dict(request.headers)
    parsed = scm_provider.parse_webhook(headers, raw_body)
    if parsed is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "unsupported or unparseable webhook event",
        )

    with database.transaction() as session:
        integration = session.scm_integration_for_repository(provider_type, parsed.repository_identity)
        if integration is None or not integration.enabled:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND,
                f"repository {parsed.repository_identity} is not mapped to any active application",
            )

        # Signature verification against server-side secret
        verified = scm_provider.verify_webhook(
            headers,
            raw_body,
            secret_token=integration.secret_token,
            secret_token_hash=integration.secret_token_hash,
        )
        if not verified:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid webhook signature")

        # Atomic deduplication on delivery ID
        delivery = ScmWebhookDelivery(
            delivery_id=parsed.delivery_id,
            provider=provider_type,
            event_type=parsed.event_type,
            repository_identity=parsed.repository_identity,
            application_id=integration.application_id,
            commit_sha=parsed.commit_sha,
            status="received",
        )
        recorded = session.record_scm_webhook_delivery(delivery)
        if not recorded:
            return JSONResponse(
                status_code=status.HTTP_200_OK,
                content={
                    "status": "ignored_duplicate",
                    "deliveryId": parsed.delivery_id,
                    "message": "webhook delivery was already processed",
                },
            )

        app = session.application(integration.application_id)
        if app is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "associated application not found")

    # Which rules apply is the module's configuration, read now (ADR-043). An application
    # with no Portal module -- one created through the delivery API alone -- has no rules
    # of its own and gets the defaults for its one environment.
    managed_parameters: dict[str, object] = {}
    if integration.credential_reference:
        managed_parameters["credentialsId"] = integration.credential_reference
    module = portal.module_for_application(app.id)
    module_id = str(module["id"]) if module else None
    try:
        rules = (portal.delivery_rules(module_id) if module_id
                 else default_delivery_rules(app.default_environment, [app.default_environment]))
    except PortalError as exc:
        raise HTTPException(status_code=exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc
    event = ScmEvent(
        kind=parsed.kind,  # type: ignore[arg-type]
        branch=(parsed.base_branch or parsed.branch) if parsed.kind == "pull_request" else parsed.branch,
        tag=parsed.tag,
        from_fork=parsed.from_fork,
    )
    decision = decide_trigger(rules, event)
    trigger = {
        "event": parsed.kind,
        "ref": parsed.ref,
        "branch": parsed.branch,
        "tag": parsed.tag,
        "pullRequest": parsed.pull_request_number,
        "baseBranch": parsed.base_branch,
        "fromFork": parsed.from_fork,
        "sender": parsed.sender,
        "rule": decision.rule_index,
        "reason": decision.reason,
        "deliveryId": parsed.delivery_id,
    }
    if not decision.run:
        # Answered 200: the delivery was received and understood; there is nothing to do.
        # A 4xx would make the SCM mark the hook as failing and retry it.
        logger.info("scm webhook ignored", extra={"deliveryId": parsed.delivery_id, "reason": decision.reason})
        return JSONResponse(status_code=status.HTTP_200_OK, content={
            "status": "ignored", "deliveryId": parsed.delivery_id, "reason": decision.reason,
        })

    # A build that deploys runs in the environment it deploys to; one that does not is
    # labelled with the first environment it could reach, which is where a promotion of
    # it would start.
    environment = decision.deploy_to or app.default_environment
    config_revision_id = None
    try:
        if module_id is None:
            parameters = managed_parameters
        elif decision.deploy_to is not None:
            parameters = portal.delivery_parameters(module_id, environment, managed_parameters)
        else:
            parameters = portal.build_parameters(module_id, managed_parameters)
        if module and module.get("activeConfigRevisionId"):
            config_revision_id = UUID(str(module["activeConfigRevisionId"]))
    except PortalError as exc:
        raise HTTPException(status_code=exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc

    corr_id = str(getattr(request.state, "correlation_id", None) or uuid4())
    run = platform.start_pipeline(
        app.id,
        commit_sha=parsed.commit_sha,
        branch=parsed.branch,
        environment=environment,
        parameters=parameters,
        correlation_id=corr_id,
        idempotency_key=f"scm:{parsed.delivery_id}",
        started_by=f"scm:{provider_type.value}:{parsed.sender}",
        config_revision_id=config_revision_id,
        deploy_after_build=decision.deploy_to is not None,
        publish_artifact=decision.publish,
        release_tag=decision.release_tag,
        trigger=trigger,
    )

    return JSONResponse(
        status_code=status.HTTP_201_CREATED,
        content={
            "status": "triggered",
            "deliveryId": parsed.delivery_id,
            "pipelineRunId": str(run.id),
            "commitSha": parsed.commit_sha,
            "branch": parsed.branch,
            "decision": {
                "reason": decision.reason,
                "deployTo": decision.deploy_to.value if decision.deploy_to else None,
                "publish": decision.publish,
                "registerVersion": decision.release_tag,
            },
        },
    )


def _require_netbox_webhook_signature(request: Request, body: bytes) -> None:
    """NetBox signs each webhook with HMAC-SHA512 of the raw body in `X-Hook-Signature`.

    Fails closed when the secret is not configured: outside local mode an unsigned
    endpoint that can block or unblock deployments is worse than no endpoint, and
    silently accepting everything is indistinguishable from having configured it.
    """

    secret = os.getenv("NETCI_NETBOX_WEBHOOK_SECRET", "").strip()
    if not secret:
        if is_local_runtime():
            return
        raise HTTPException(
            status.HTTP_501_NOT_IMPLEMENTED,
            detail={
                "code": "NETBOX_WEBHOOK_SECRET_NOT_CONFIGURED",
                "message": "set NETCI_NETBOX_WEBHOOK_SECRET to the secret configured on the "
                           "NetBox webhook; netCI will not act on an unsigned DCIM event",
            },
        )

    supplied = (request.headers.get("x-hook-signature") or "").strip()
    expected = hmac.new(secret.encode(), body, hashlib.sha512).hexdigest()
    if not supplied or not secrets.compare_digest(supplied.lower(), expected):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            detail={"code": "INVALID_WEBHOOK_SIGNATURE",
                    "message": "X-Hook-Signature does not match the configured secret"},
        )


@app.post("/webhooks/dcim/netbox", status_code=status.HTTP_200_OK)
@app.post("/webhooks/netbox", status_code=status.HTTP_200_OK)
async def receive_netbox_webhook(request: Request) -> dict[str, object]:
    """Bidirectional webhook from NetBox DCIM: locks or unlocks targets when device status changes.

    Signed, because of what it decides. This endpoint took an unauthenticated body and
    acted on it: anyone who could reach it could put a production host into maintenance
    and block every deployment to it, or -- worse -- clear the maintenance flag on a host
    an operator had deliberately taken out of service, and watch deployments land on it.
    The SCM webhook next door has always verified a signature against a server-side
    secret; this one is held to the same bar.
    """

    raw_body = await request.body()
    if len(raw_body) > MAX_WEBHOOK_PAYLOAD_BYTES:
        raise HTTPException(
            status.HTTP_413_CONTENT_TOO_LARGE,
            "webhook payload exceeds maximum permitted size of 1MB",
        )
    _require_netbox_webhook_signature(request, raw_body)

    try:
        payload = json.loads(raw_body)
    except Exception as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"invalid JSON payload: {exc}") from exc

    event = str(payload.get("event") or "").strip().lower()
    data = payload.get("data")
    if not isinstance(data, dict):
        return {"status": "ignored", "reason": "missing data block"}

    server_name = str(data.get("name") or "").strip()
    if not server_name:
        return {"status": "ignored", "reason": "no device name in data"}

    raw_status = data.get("status")
    status_str = (
        raw_status.get("value")
        if isinstance(raw_status, dict)
        else str(raw_status or "")
    ).strip().lower()

    # ADR-015: the server decides the actor. `username` arrives in the body, so it is
    # what the caller claims, not who acted -- recording it as the operator would put a
    # caller-chosen name in the maintenance audit trail.
    claimed_user = str(payload.get("username") or "")
    blocking_statuses = {"offline", "failed", "decommissioning", "maintenance", "staged", "planned"}

    if event == "deleted" or status_str in blocking_statuses:
        reason = f"NetBox DCIM event '{event}': device status is '{status_str}'" + (
            f" (reported by {claimed_user})" if claimed_user else ""
        )
        state = set_server_maintenance(server_name, in_maintenance=True, reason=reason, operator="netbox-webhook")
        logger.warning("NetBox Webhook: locked server %s from deployments (%s)", server_name, reason)
        return {
            "status": "ok",
            "action": "locked",
            "server": server_name,
            "inMaintenance": state.in_maintenance,
            "reason": reason,
        }
    elif status_str == "active":
        state = set_server_maintenance(server_name, in_maintenance=False, reason="NetBox DCIM status: active", operator="netbox-webhook")
        logger.info("NetBox Webhook: unlocked server %s for deployments", server_name)
        return {
            "status": "ok",
            "action": "unlocked",
            "server": server_name,
            "inMaintenance": state.in_maintenance,
        }

    return {"status": "ok", "action": "noop", "server": server_name, "device_status": status_str}


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


@app.get("/pipeline-runs/{pipelineRunId}/logs/stream")
async def stream_pipeline_logs(pipelineRunId: UUID, principal: Principal = ReadAccess):
    """Server-Sent Events (SSE) live log stream for real-time console rendering."""
    run = platform.get_pipeline(pipelineRunId)
    _require_application_access(platform.get_application(run.application_id), principal)

    async def event_generator():
        last_index = 0
        terminal_statuses = {"succeeded", "failed", "cancelled", "rolled_back"}
        ticks = 0
        while ticks < 240:  # stream for up to 120s
            try:
                current_run, lines = platform.get_pipeline_logs(pipelineRunId)
                lines_list = list(lines)
                new_lines = lines_list[last_index:]
                if new_lines:
                    for line in new_lines:
                        payload = json.dumps({"line": line, "status": current_run.status})
                        yield f"data: {payload}\n\n"
                    last_index = len(lines_list)
                if current_run.status in terminal_statuses:
                    yield f"data: {json.dumps({'status': current_run.status, 'done': True})}\n\n"
                    break
            except Exception as e:
                yield f"data: {json.dumps({'error': str(e)})}\n\n"
                break
            await asyncio.sleep(0.5)
            ticks += 1

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )



@app.post("/pipeline-runs/{pipelineRunId}/cancel", status_code=status.HTTP_200_OK)
def cancel_pipeline_run(
    pipelineRunId: UUID,
    payload: CancelRequest | None = None,
    principal: Principal = DeveloperAccess,
) -> dict[str, object]:
    run = platform.get_pipeline(pipelineRunId)
    _require_application_access(platform.get_application(run.application_id), principal)
    reason = payload.reason if payload else ""
    updated = platform.cancel_pipeline(pipelineRunId, actor=principal.subject, reason=reason)
    return pipeline_json(updated)


@app.post("/pipeline-runs/{pipelineRunId}/retry", status_code=status.HTTP_201_CREATED)
def retry_pipeline_run(
    pipelineRunId: UUID,
    request: Request,
    principal: Principal = DeveloperAccess,
) -> dict[str, object]:
    parent = platform.get_pipeline(pipelineRunId)
    _require_application_access(platform.get_application(parent.application_id), principal)
    _require_environment_role(parent.environment, principal)
    idempotency_key = request.headers.get("Idempotency-Key")
    # Re-bind to the module's current targets and active revision (see retry_pipeline).
    parameters: dict[str, object] | None = None
    active_rev_id: UUID | None = None
    module = portal.module_for_application(parent.application_id)
    if module is not None:
        supplied = {
            key: value for key, value in parent.parameters.items()
            if key not in SERVER_OWNED_DELIVERY_KEYS
        }
        try:
            parameters = portal.delivery_parameters(str(module["id"]), parent.environment, supplied)
        except PortalError as exc:
            raise HTTPException(status_code=exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc
        if module.get("activeConfigRevisionId"):
            active_rev_id = UUID(str(module["activeConfigRevisionId"]))
    new_run = platform.retry_pipeline(
        pipelineRunId, actor=principal.subject, idempotency_key=idempotency_key,
        parameters=parameters, config_revision_id=active_rev_id,
    )
    return pipeline_json(new_run)


@app.get("/pipeline-runs/{pipelineRunId}/stages")
def get_pipeline_stages(pipelineRunId: UUID, principal: Principal = ReadAccess) -> dict[str, object]:
    run = platform.get_pipeline(pipelineRunId)
    _require_application_access(platform.get_application(run.application_id), principal)
    stages = platform.list_pipeline_stages(pipelineRunId)
    return {"pipelineRunId": str(pipelineRunId), "items": [stage_json(s) for s in stages]}


@app.post("/pipeline-runs/{pipelineRunId}/stages", status_code=status.HTTP_202_ACCEPTED)
def record_pipeline_stage_result(
    pipelineRunId: UUID,
    payload: StageResultRequest,
    request: Request,
    _: Principal = PipelineAccess,
) -> dict[str, object]:
    _authorize_callback(
        request,
        scope=(Scope.CI_STAGE, Scope.CI_RESULT),
        workload=Workload.JENKINS,
        pipeline_run_id=pipelineRunId,
    )
    stage_name = payload.stageName or payload.stageId or "stage"
    stage_id = payload.stageId or stage_name.lower().replace(" ", "-")
    saved = platform.record_stage_event(
        pipelineRunId,
        stage_id=stage_id,
        stage_name=stage_name,
        status=payload.status,
        attempt=payload.attempt,
        queued_at=payload.queuedAt,
        started_at=payload.startedAt,
        completed_at=payload.completedAt,
        duration_ms=payload.durationMs,
        error_message=payload.errorMessage,
        log_snippet=payload.logSnippet,
    )
    return stage_json(saved)


@app.post("/pipeline-runs/{pipelineRunId}/stages/{stageId}", status_code=status.HTTP_202_ACCEPTED)
def record_pipeline_stage_result_by_id(
    pipelineRunId: UUID,
    stageId: str,
    payload: StageResultRequest,
    request: Request,
    _: Principal = PipelineAccess,
) -> dict[str, object]:
    _authorize_callback(
        request,
        scope=(Scope.CI_STAGE, Scope.CI_RESULT),
        workload=Workload.JENKINS,
        pipeline_run_id=pipelineRunId,
    )
    stage_name = payload.stageName or stageId
    saved = platform.record_stage_event(
        pipelineRunId,
        stage_id=stageId,
        stage_name=stage_name,
        status=payload.status,
        attempt=payload.attempt,
        queued_at=payload.queuedAt,
        started_at=payload.startedAt,
        completed_at=payload.completedAt,
        duration_ms=payload.durationMs,
        error_message=payload.errorMessage,
        log_snippet=payload.logSnippet,
    )
    return stage_json(saved)


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
        claims = _callback_claims(request)
        if claims is not None and claims.deployment_id is not None and claims.pipeline_run_id is None:
            # A deployment token names the deployment, not the run that built it. The
            # worker re-verifies evidence for *that* run before deploying, so the binding
            # is derived: the token is good for exactly the run its deployment came from.
            deployment = platform.get_deployment(claims.deployment_id)
            # During a rollback the deployment is moving to an older digest, built by an
            # older run of the same application; the worker re-verifies *that* one. The
            # binding is still derived from what the deployment currently carries -- a
            # token can never read evidence for a digest its deployment is not moving to.
            rolling_back = deployment.status in {
                DeploymentStatus.ROLLBACK_IN_PROGRESS,
                DeploymentStatus.ROLLED_BACK,
                DeploymentStatus.ROLLBACK_FAILED,
            }
            built_current_artifact = (
                rolling_back
                and run.application_id == deployment.application_id
                and run.artifact_digest == deployment.artifact_digest
            )
            if deployment.pipeline_run_id != pipelineRunId and not built_current_artifact:
                raise HTTPException(
                    status_code=403,
                    detail={
                        "code": "RESOURCE_MISMATCH",
                        "message": "this deployment token is not for the run that built this evidence",
                    },
                )
            _authorize_callback(request, scope=Scope.CI_EVIDENCE, deployment_id=claims.deployment_id)
        else:
            _authorize_callback(request, scope=Scope.CI_EVIDENCE, pipeline_run_id=pipelineRunId)
    return platform.security_evidence(pipelineRunId)


def _findings_from_evidence(
    evidence: dict[str, Any], digest: str, now: datetime
) -> list[ArtifactFindingRecord]:
    """The build's own blocking findings, in the shape `VulnerabilityScanEvidence` accepts."""

    scan = evidence.get("vulnerabilityScan")
    raw_list = (scan.get("findings") or []) if isinstance(scan, dict) else []
    return [
        ArtifactFindingRecord(
            artifact_digest=digest, source="ci", vulnerability_id=str(entry["id"]).strip(),
            severity=str(entry.get("severity") or "UNKNOWN").strip().upper(),
            package=str(entry.get("package") or ""), installed_version=str(entry.get("installedVersion") or ""),
            fixed_version=str(entry.get("fixedVersion") or ""), first_seen_at=now, last_seen_at=now,
        )
        for entry in raw_list
        if isinstance(entry, dict) and str(entry.get("id") or "").strip()
    ]


@app.post("/pipeline-runs/{pipelineRunId}/sbom", status_code=status.HTTP_202_ACCEPTED)
async def record_pipeline_sbom(
    pipelineRunId: UUID,
    request: Request,
    _: Principal = PipelineAccess,
) -> dict[str, object]:
    _authorize_callback(request, scope=Scope.CI_EVIDENCE, pipeline_run_id=pipelineRunId)
    raw = await request.body()
    if len(raw) > 16 * 1024 * 1024:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail={"code": "SBOM_TOO_LARGE", "message": "SBOM payload exceeds maximum permitted size of 16MB"},
        )
    try:
        document = json.loads(raw)
    except (json.JSONDecodeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "INVALID_SBOM", "message": "SBOM payload is not valid JSON"},
        ) from exc
    if (
        not isinstance(document, dict)
        or document.get("bomFormat") != "CycloneDX"
        or not isinstance(document.get("components"), list)
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "INVALID_SBOM", "message": "document must be a CycloneDX SBOM with a components list"},
        )

    run = platform.get_pipeline(pipelineRunId)
    try:
        evidence = platform.security_evidence(pipelineRunId)
    except DeliveryError:
        evidence = None
    digest = str(evidence.get("artifactDigest") or "") if evidence else ""
    if not digest:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "EVIDENCE_REQUIRED_FIRST",
                "message": "publish security evidence before the SBOM: the SBOM is recorded against the digest that evidence names",
            },
        )

    now = datetime.now(timezone.utc)
    components = document["components"]
    with database.transaction() as tx:
        record = ArtifactSbomRecord(
            artifact_digest=digest,
            application_id=run.application_id,
            pipeline_run_id=pipelineRunId,
            format="cyclonedx-json",
            document=document,
            component_count=len(components),
            recorded_at=now,
        )
        recorded = tx.record_artifact_sbom(record)
        ci_findings = _findings_from_evidence(evidence or {}, digest, now)
        tx.replace_artifact_findings(digest, "ci", ci_findings, now)

    return {
        "artifactDigest": digest,
        "components": len(components),
        "recorded": recorded,
    }


# Plain `def`: these read the store synchronously, and an `async def` would run that on
# the event loop and stall every other request while it did.
@app.get("/vulnerabilities/exposure")
def list_running_vulnerabilities(
    minSeverity: str = "HIGH",
    _: Principal = ReadAccess,
) -> dict[str, object]:
    sev_key = minSeverity.strip().upper()
    if sev_key not in SEVERITY_ORDER:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "INVALID_SEVERITY",
                "message": f"minSeverity must be one of: {', '.join(SEVERITY_ORDER.keys())} (got {minSeverity!r})",
            },
        )
    app_ids = _exposure_application_ids(_)
    return vulnerability_exposure(platform, min_severity=sev_key, application_ids=app_ids)


@app.get("/vulnerabilities/{vulnerabilityId}/exposure")
def get_vulnerability_exposure(
    vulnerabilityId: str,
    _: Principal = ReadAccess,
) -> dict[str, object]:
    if not re.fullmatch(r"^[A-Za-z0-9][A-Za-z0-9._:-]{2,63}$", vulnerabilityId):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "INVALID_VULNERABILITY_ID",
                "message": f"vulnerabilityId must match ^[A-Za-z0-9][A-Za-z0-9._:-]{{2,63}}$ (got {vulnerabilityId!r})",
            },
        )
    app_ids = _exposure_application_ids(_)
    return vulnerability_exposure(platform, vulnerability_id=vulnerabilityId, application_ids=app_ids)


@app.post("/vulnerabilities/rescan")
async def rescan_vulnerabilities(_: Principal = AdminAccess) -> dict[str, object]:
    outcome = await asyncio.to_thread(rescan_in_service, platform, sbom_scanner)
    return outcome


class ChangeFreezeCreate(StrictBody):
    name: str = Field(min_length=1, max_length=120)
    startsAt: datetime
    endsAt: datetime
    environments: list[Environment] = Field(min_length=1, max_length=3)
    reason: str = Field(min_length=1, max_length=1000)
    systemId: str | None = Field(default=None, pattern=r"^[a-zA-Z][a-zA-Z0-9-]{2,62}$")
    moduleId: str | None = Field(default=None, pattern=r"^[a-z0-9][a-z0-9-]{2,62}$")

    @model_validator(mode="after")
    def validate_freeze(self) -> "ChangeFreezeCreate":
        if self.startsAt.tzinfo is None or self.startsAt.tzinfo.utcoffset(self.startsAt) is None:
            raise ValueError("startsAt must be timezone-aware")
        if self.endsAt.tzinfo is None or self.endsAt.tzinfo.utcoffset(self.endsAt) is None:
            raise ValueError("endsAt must be timezone-aware")
        if self.endsAt <= self.startsAt:
            raise ValueError("endsAt must be after startsAt")
        if self.endsAt - self.startsAt > timedelta(days=31):
            raise ValueError("freeze duration cannot exceed 31 days")
        if len(self.environments) != len(set(self.environments)):
            raise ValueError("duplicate environments are not allowed")
        if self.systemId is not None and self.moduleId is not None:
            raise ValueError("cannot specify both systemId and moduleId")
        return self


def change_freeze_json(item: ChangeFreezeRecord) -> dict[str, Any]:
    return {
        "id": str(item.id),
        "name": item.name,
        "startsAt": item.starts_at.isoformat(),
        "endsAt": item.ends_at.isoformat(),
        "environments": list(item.environments),
        "systemId": item.system_id,
        "moduleId": item.module_id,
        "reason": item.reason,
        "createdBy": item.created_by,
        "createdAt": item.created_at.isoformat(),
        "cancelledAt": item.cancelled_at.isoformat() if item.cancelled_at else None,
        "cancelledBy": item.cancelled_by,
    }


@app.post("/change-freezes", status_code=status.HTTP_201_CREATED)
def create_change_freeze(
    payload: ChangeFreezeCreate,
    principal: Principal = ReviewerAccess,
) -> dict[str, Any]:
    if payload.moduleId:
        try:
            portal.module(payload.moduleId)
        except KeyError as exc:
            raise HTTPException(
                status_code=404,
                detail={"code": "MODULE_NOT_FOUND", "message": f"Module {payload.moduleId} not found"},
            ) from exc
    if payload.systemId:
        try:
            portal.system(payload.systemId)
        except KeyError as exc:
            raise HTTPException(
                status_code=404,
                detail={"code": "SYSTEM_NOT_FOUND", "message": f"System {payload.systemId} not found"},
            ) from exc
    record = ChangeFreezeRecord(
        id=uuid4(),
        name=payload.name,
        starts_at=payload.startsAt,
        ends_at=payload.endsAt,
        environments=tuple(e.value for e in payload.environments),
        reason=payload.reason,
        created_by=principal.subject,
        system_id=payload.systemId,
        module_id=payload.moduleId,
    )
    with database.transaction() as tx:
        tx.insert_change_freeze(record)
        tx.apply(
            UnitOfWork(
                audit=[
                    AuditRecord(
                        "change_freeze.created",
                        actor=principal.subject,
                        payload={
                            "freezeId": str(record.id),
                            "name": record.name,
                            "startsAt": record.starts_at.isoformat(),
                            "endsAt": record.ends_at.isoformat(),
                            "environments": list(record.environments),
                        },
                    )
                ]
            )
        )
    return change_freeze_json(record)


@app.get("/change-freezes")
def list_change_freezes(
    includePast: bool = False,
    _: Principal = ReadAccess,
) -> dict[str, Any]:
    with database.transaction() as tx:
        items = tx.change_freezes(ending_after=None if includePast else datetime.now(timezone.utc))
    return {"items": [change_freeze_json(i) for i in items]}


@app.post("/change-freezes/{freezeId}/cancel")
def cancel_change_freeze(
    freezeId: UUID,
    principal: Principal = ReviewerAccess,
) -> dict[str, Any]:
    with database.transaction() as tx:
        existing = tx.change_freeze(freezeId)
        if existing is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "FREEZE_NOT_FOUND", "message": f"Change freeze {freezeId} not found"},
            )
        now = datetime.now(timezone.utc)
        if not tx.cancel_change_freeze(freezeId, cancelled_by=principal.subject, at=now):
            raise HTTPException(
                status_code=409,
                detail={"code": "FREEZE_NOT_ACTIVE", "message": f"Change freeze {freezeId} is not active"},
            )
        tx.apply(
            UnitOfWork(
                audit=[
                    AuditRecord(
                        "change_freeze.cancelled",
                        actor=principal.subject,
                        payload={
                            "freezeId": str(freezeId),
                            "name": existing.name,
                            "startsAt": existing.starts_at.isoformat(),
                            "endsAt": existing.ends_at.isoformat(),
                            "environments": list(existing.environments),
                        },
                    )
                ]
            )
        )
        updated = tx.change_freeze(freezeId)
        assert updated is not None
        return change_freeze_json(updated)


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


@app.post("/pipeline-runs/{pipelineRunId}/approve", status_code=status.HTTP_202_ACCEPTED, response_model=None)
def approve_pipeline_run_deployment(
    pipelineRunId: UUID, payload: ApprovalRequest | None = None, principal: Principal = ReviewerAccess
) -> JSONResponse | dict[str, object]:
    """Release a deployment waiting for human approval associated with a pipeline run."""
    run = platform.get_pipeline(pipelineRunId)
    _require_environment_role(run.environment, principal)
    _require_application_access(platform.get_application(run.application_id), principal)

    with database.transaction() as session:
        deps = session.deployments(pipeline_run_id=pipelineRunId)
    pending = [d for d in deps if d.status == DeploymentStatus.PENDING_APPROVAL]
    if not pending:
        raise HTTPException(
            status_code=404, detail={"code": "NO_PENDING_DEPLOYMENT", "message": "no pending deployment for this run"}
        )

    deployment_id = pending[0].id
    if separation_of_duties_enabled(principal):
        try:
            require_separation_of_duties(platform.deployment_requested_by(deployment_id), principal.subject)
        except PolicyViolation as exc:
            raise HTTPException(
                status_code=403, detail={"code": "SEPARATION_OF_DUTIES", "message": str(exc)}
            ) from exc
    return deployment_json(platform.approve_deployment(deployment_id, principal.subject))


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
        terminal=True,
    )
    deployment = platform.record_deployment_result(
        deploymentId, payload.status, payload.message, fencing_token=payload.fencingToken
    )
    portal.record_production_deployment_result(deploymentId, payload.status, payload.message)
    return deployment_json(deployment)


@app.post("/deployments/{deploymentId}/heartbeat", status_code=status.HTTP_200_OK)
def heartbeat_deployment(
    deploymentId: UUID,
    payload: HeartbeatRequest,
    request: Request,
    _: Principal = PipelineAccess,
) -> dict[str, object]:
    """Extend a running deployment's hold on its target.

    A worker that stops heartbeating loses the target to the next deployment. That is the
    point: a lease with no expiry is a deadlock waiting for a worker to crash, and one
    that expires while work is genuinely in flight is a collision.
    """

    _authorize_callback(
        request,
        scope=Scope.DEPLOYMENT_RESULT,
        workload=Workload.TEMPORAL,
        deployment_id=deploymentId,
    )
    return platform.heartbeat_deployment(deploymentId, payload.fencingToken)


@app.post("/deployments/{deploymentId}/rollback-result", status_code=status.HTTP_202_ACCEPTED)
def record_rollback_result(
    deploymentId: UUID,
    payload: RollbackResultRequest,
    request: Request,
    _: Principal = PipelineAccess,
) -> dict[str, object]:
    """Report whether a rollback actually restored service.

    `rollback_failed` exists because a rollback that did not work leaves the environment
    in neither the new state nor the old one, and calling that `failed` loses the fact
    that recovery was attempted. It also emits no DORA recovery event: nothing was
    restored, and a metric that says otherwise is worse than no metric.
    """

    _authorize_callback(
        request,
        scope=Scope.DEPLOYMENT_RESULT,
        workload=Workload.TEMPORAL,
        deployment_id=deploymentId,
        terminal=True,
    )
    return deployment_json(
        platform.record_rollback_result(
            deploymentId, payload.succeeded, payload.message, fencing_token=payload.fencingToken
        )
    )


@app.post("/deployment-leases/recover", status_code=status.HTTP_200_OK)
def recover_deployment_leases(_: Principal = AdminAccess) -> dict[str, object]:
    """Release leases whose owner stopped heartbeating, and record that it happened.

    This is not what unblocks a target -- acquisition reclaims an expired lease at the
    moment somebody wants it, so there is no window where a dead lease blocks work. This
    exists so an operator can see that a worker died without waiting for the next
    deployment to reveal it.
    """

    recovered = platform.recover_expired_leases()
    return {"recovered": len(recovered), "items": recovered}


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


@app.post("/deployments/{deploymentId}/cancel", status_code=status.HTTP_200_OK)
def cancel_deployment(
    deploymentId: UUID,
    payload: CancelRequest | None = None,
    principal: Principal = DeveloperAccess,
) -> dict[str, object]:
    deployment = platform.get_deployment(deploymentId)
    _require_application_access(
        platform.get_application(deployment.application_id), principal
    )
    reason = payload.reason if payload else ""
    updated = platform.cancel_deployment(deploymentId, actor=principal.subject, reason=reason)
    portal.record_production_deployment_cancelled(deploymentId, principal.subject, reason)
    return deployment_json(updated)


@app.get("/api/v1/ci/controllers/drift")
def ci_controller_drift(_: Principal = AdminAccess) -> dict[str, object]:
    """Whether the Jenkins controllers run the same configuration.

    Multi-controller operation assumes every controller was rebuilt from the same
    git-managed JCasC; this compares what each one actually loaded (normalized export,
    plugin set, non-netCI jobs) so a controller someone changed by hand is visible.
    """

    probe = getattr(platform.ci_launcher, "controller_drift", None)
    if probe is None:
        return {"mode": getattr(platform.ci_launcher, "mode", "none"), "controllers": {}, "drift": False, "differing": [], "unreachable": {}}
    return {"mode": "jenkins", **probe()}


def _casc_webhook_signature_ok(request: Request, body: bytes) -> bool:
    """A git host's push webhook, authenticated by HMAC over the body.

    GitHub's `X-Hub-Signature-256: sha256=<hex>` and a generic `X-NetCI-Signature` are
    accepted. Without a configured secret no signature is valid: a reload is an
    operator action, and an unauthenticated one would let anyone on the network make
    every controller re-read its configuration at will.
    """

    secret = os.getenv("NETCI_CASC_WEBHOOK_SECRET", "").strip()
    supplied = request.headers.get("x-hub-signature-256") or request.headers.get("x-netci-signature") or ""
    if not secret or not supplied.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return secrets.compare_digest(supplied[len("sha256="):].strip().lower(), expected)


@app.post("/api/v1/ci/controllers/reload")
async def ci_controllers_reload(request: Request) -> dict[str, object]:
    """Make every Jenkins controller re-read its git-managed JCasC, then compare them.

    Called by the configuration repository's push webhook (HMAC) or by a platform
    admin (bearer). Jenkins configuration is rebuilt or reloaded from git, never edited
    in place (ADR-033); this is the reload half.
    """

    body = await request.body()
    actor: str
    if _casc_webhook_signature_ok(request, body):
        actor = "webhook:casc"
    else:
        principal = current_principal(request, request.headers.get("authorization"))
        if not principal.has_any(Role.PLATFORM_ADMIN):
            raise HTTPException(
                status_code=401 if principal.is_anonymous else 403,
                detail={"code": "CASC_RELOAD_UNAUTHORIZED", "message": "a valid webhook signature or a platform-admin token is required"},
            )
        actor = principal.subject
    reload = getattr(platform.ci_launcher, "reload_controllers", None)
    if reload is None:
        raise HTTPException(status_code=501, detail={"code": "CI_NOT_JENKINS", "message": "no Jenkins controllers are configured"})
    outcome = await asyncio.to_thread(reload)
    with database.transaction() as session:
        session.apply(UnitOfWork(audit=[AuditRecord(
            "ci.controllers.reloaded", actor=actor,
            payload={"controllers": outcome["controllers"], "drift": outcome["drift"]["drift"], "differing": outcome["drift"]["differing"]},
        )]))
    return {"mode": "jenkins", "actor": actor, **outcome}


@app.post("/reconciler/reconcile", status_code=status.HTTP_200_OK)
def trigger_reconciliation(
    payload: ReconcileRequest | None = None,
    _: Principal = AdminAccess,
) -> dict[str, object]:
    limit = payload.limit if payload else 50
    timeout_seconds = payload.timeoutSeconds if payload else None
    return reconciler.reconcile(limit=limit, timeout_seconds=timeout_seconds)


# ----------------------------------------------------------- governance & policy

@app.get("/policy/decisions")
def list_policy_decisions(
    scope: str | None = None,
    targetType: str | None = None,
    targetId: str | None = None,
    limit: int | None = None,
    cursor: str | None = None,
    principal: Principal = ReadAccess,
) -> dict[str, object]:
    bounded_limit = max(1, min(limit or 50, 100))
    with database.transaction() as session:
        records, next_cursor, has_more = session.policy_decisions_paginated(
            scope=scope, target_type=targetType, target_id=targetId, limit=bounded_limit, cursor=cursor
        )
    return {
        "items": [policy_decision_json(r) for r in records],
        "nextCursor": next_cursor,
        "hasMore": has_more,
    }


@app.get("/security-exceptions")
def list_security_exceptions(
    activeOnly: bool = False,
    principal: Principal = ReadAccess,
) -> list[dict[str, object]]:
    with database.transaction() as session:
        records = session.security_exceptions(active_only=activeOnly)
    return [security_exception_json(r) for r in records]


@app.post("/security-exceptions", status_code=status.HTTP_201_CREATED)
def create_security_exception(
    payload: SecurityExceptionCreate,
    principal: Principal = ReviewerAccess,
) -> dict[str, object]:
    now = datetime.now(timezone.utc)
    if payload.expiresAt <= now:
        raise HTTPException(
            status_code=422,
            detail={"code": "EXPIRED_DATE", "message": "Security exception expiresAt must be in the future"},
        )
    if separation_of_duties_enabled(principal) and principal.subject == payload.owner:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "SEPARATION_OF_DUTIES",
                "message": "The owner of a security exception cannot approve their own exception",
            },
        )
    record = SecurityExceptionRecord(
        id=uuid4(),
        cve=payload.cve.upper(),
        artifact_digest=payload.artifactDigest,
        owner=payload.owner,
        reason=payload.reason,
        approved_by=principal.subject,
        status="active",
        created_at=now,
        expires_at=payload.expiresAt,
    )
    with database.transaction() as session:
        session.insert_security_exception(record)
    return security_exception_json(record)


@app.post("/security-exceptions/{exceptionId}/revoke", status_code=status.HTTP_200_OK)
def revoke_security_exception(
    exceptionId: UUID,
    principal: Principal = ReviewerAccess,
) -> dict[str, object]:
    now = datetime.now(timezone.utc)
    with database.transaction() as session:
        success = session.revoke_security_exception(
            exception_id=exceptionId,
            revoked_by=principal.subject,
            revoked_at=now,
        )
    if not success:
        raise HTTPException(
            status_code=404,
            detail={"code": "EXCEPTION_NOT_FOUND", "message": f"Active security exception {exceptionId} not found"},
        )
    return {"id": str(exceptionId), "status": "revoked", "revokedBy": principal.subject}


_ACTIVE_RUNNERS: dict[str, WebSocket] = {}


def _agent_dispatch_interval_seconds() -> float:
    raw = os.getenv("NETCI_AGENT_DISPATCH_INTERVAL_SECONDS", "0.5").strip()
    try:
        return max(0.1, float(raw))
    except ValueError:
        return 0.5


async def _dispatch_agent_commands(hostname: str, websocket: WebSocket) -> None:
    """Forward commands claimed from the database down this agent's socket.

    Runs for as long as the socket lives. A command submitted on any replica lands in
    `agent_commands`; this replica, holding the socket, is the only one that can claim
    it for this hostname, and the claim is exactly-once by construction.
    """

    interval = _agent_dispatch_interval_seconds()
    while True:
        try:
            claimed = await asyncio.to_thread(fleet.claim, [hostname])
            for command in claimed:
                await websocket.send_text(json.dumps({"type": "EXEC_COMMAND", "task_id": str(command.id), "command": command.command}))
        except (WebSocketDisconnect, RuntimeError):
            return
        except Exception:  # noqa: BLE001 - a transient database error must not end the socket
            logger.exception("agent command dispatch for %s failed; retrying", hostname)
        await asyncio.sleep(interval)


@app.websocket("/api/v1/agents/ws")
async def runner_agent_websocket(
    websocket: WebSocket,
    agent_id: str = "runner",
    token: str = "",
):
    """Accept an edge runner that connected outbound, once it has proved who it is.

    This used to `accept()` unconditionally and take the hostname from the query string.
    Anyone who could reach the API could register as any host -- and because execute
    matched hostnames by substring, an agent named `a` received the commands meant for
    every host containing an `a`. The hostname now comes from a signed agent token; the
    query string cannot choose it.
    """

    supplied = token or _bearer(websocket.headers.get("authorization"))
    try:
        claims = workload_identity.verify(supplied) if supplied else None
    except WorkloadIdentityError as exc:
        logger.warning("agent connection refused: %s", exc.code)
        await websocket.close(code=4401, reason=exc.code)
        return
    if (
        claims is None
        or claims.workload != Workload.AGENT
        or not claims.permits(Scope.AGENT_CONNECT)
        or not claims.agent_hostname
    ):
        await websocket.close(code=4403, reason="AGENT_TOKEN_REQUIRED")
        return
    host_key = claims.agent_hostname
    # The connection is a row every replica can read; the socket stays here. The row is
    # written before the socket is accepted: an agent that cannot be recorded is refused
    # (and retries), never connected-but-invisible.
    try:
        await asyncio.to_thread(fleet.register, host_key, agent_id, claims.jti)
    except Exception:  # noqa: BLE001 - the database is the failure to report here
        logger.exception("agent %s could not be registered; refusing the connection", host_key)
        await websocket.close(code=1013, reason="AGENT_REGISTRY_UNAVAILABLE")
        return
    await websocket.accept()
    _ACTIVE_RUNNERS[host_key] = websocket
    dispatcher = asyncio.create_task(_dispatch_agent_commands(host_key, websocket))
    logger.info("Runner agent connected: %s (agent_id=%s, replica=%s)", host_key, agent_id, fleet.replica_id)
    try:
        while True:
            data = await websocket.receive_text()
            try:
                msg = json.loads(data)
                msg_type = msg.get("type")
                if msg_type == "TELEMETRY_HEARTBEAT":
                    telem = msg.get("telemetry") or {}
                    figures = [telem.get(key) for key in ("cpu_percent", "mem_percent", "disk_percent")]
                    # A heartbeat without figures keeps the connection fresh and records
                    # nothing: an unknown reading must not become "0 % used".
                    if all(isinstance(value, (int, float)) for value in figures):
                        await asyncio.to_thread(update_server_telemetry, host_key, *(float(v) for v in figures))
                    await asyncio.to_thread(fleet.touch, host_key)
                    await websocket.send_text(json.dumps({"type": "HEARTBEAT_ACK", "status": "ok"}))
                elif msg_type == "COMMAND_RESULT":
                    try:
                        command_id = UUID(str(msg.get("task_id")))
                    except ValueError:
                        continue
                    result = {
                        "output": str(msg.get("output", ""))[:65536],
                        "exitCode": int(msg.get("exit_code", 0) or 0),
                        "answeredBy": fleet.replica_id,
                    }
                    await asyncio.to_thread(fleet.complete, command_id, result)
            except Exception as exc:
                logger.debug("Failed parsing agent message: %s", exc)
    except WebSocketDisconnect:
        logger.info("Runner agent disconnected: %s", host_key)
    finally:
        dispatcher.cancel()
        _ACTIVE_RUNNERS.pop(host_key, None)
        # Synchronous on purpose. A shutdown (and the test client) delivers the
        # disconnect together with a cancellation; an `await` here is where the
        # CancelledError would land, and the row would outlive the socket. One DELETE
        # on the event loop is a few milliseconds; a stale row is a false "connected".
        try:
            fleet.unregister(host_key)
        except Exception:  # noqa: BLE001 - the socket is gone either way; say why the row is not
            logger.exception("agent %s disconnected but its connection row could not be removed", host_key)


class AgentTokenRequest(StrictBody):
    hostname: str = Field(min_length=1, max_length=253, pattern=r"^[A-Za-z0-9][A-Za-z0-9.-]*$")
    ttlSeconds: int = Field(default=86400, ge=300, le=86400)


@app.post("/api/v1/agents/token", status_code=status.HTTP_201_CREATED)
def issue_agent_token(payload: AgentTokenRequest, principal: Principal = AdminAccess) -> dict[str, object]:
    """Mint the token an edge agent presents when it connects.

    The hostname is a claim in the token, so an agent can only ever register as the host
    it was issued for. Platform-admin only: whoever can mint this decides which machine
    answers diagnostic commands for that name. Returned once, never stored.
    """

    try:
        token = workload_identity.mint(
            workload=Workload.AGENT,
            application_id=None,
            scopes={Scope.AGENT_CONNECT},
            agent_hostname=payload.hostname,
            ttl_seconds=payload.ttlSeconds,
        )
    except WorkloadIdentityError as exc:
        raise HTTPException(status_code=exc.status, detail={"code": exc.code, "message": exc.message}) from exc
    return {"token": token, "hostname": payload.hostname, "expiresInSeconds": payload.ttlSeconds}


@app.get("/api/v1/agents/status")
def get_agents_status(_: Principal = ReadAccess) -> dict[str, Any]:
    """Every agent any replica holds. Only what is known: no placeholder addresses or
    versions -- an agent that has not reported telemetry has `telemetry: null`."""

    items = []
    for connection in fleet.connections():
        telemetry = stored_server_telemetry(connection["hostname"])
        items.append({
            **connection,
            "hostKey": connection["hostname"],
            "telemetry": {
                "cpuPercent": telemetry.cpu_percent,
                "memPercent": telemetry.mem_percent,
                "diskPercent": telemetry.disk_percent,
                "observedAt": telemetry.observed_at.isoformat(),
            } if telemetry else None,
        })
    live = [item for item in items if not item["stale"]]
    return {
        "replicaId": fleet.replica_id,
        "count": len(items),
        "connectedAgents": len(live),
        "staleAgents": len(items) - len(live),
        "items": items,
        "agents": items,
    }


class AgentCommandExecute(StrictBody):
    hostname: str
    command: str
    timeout: float = 30.0


@app.post("/api/v1/agents/execute")
async def execute_agent_command(
    payload: AgentCommandExecute,
    principal: Principal = Depends(requires(Role.PLATFORM_ADMIN)),
) -> dict[str, Any]:
    """Run an allow-listed, read-only diagnostic command on a connected edge agent.

    Platform-admin only: this is a remote execution channel, however narrow the allowlist.
    The hostname must match exactly -- substring matching let one agent answer for many.
    The agent may be held by another replica: the command is a row that replica claims.
    """

    connection = next((c for c in fleet.connections() if c["hostname"] == payload.hostname), None)
    if connection is None or connection["stale"]:
        raise HTTPException(
            status_code=404,
            detail={"code": "AGENT_NOT_CONNECTED", "message": f"No active edge agent connected for {payload.hostname}"}
        )
    from .adapters.agent_daemon import validate_command_policy
    is_valid, reason = validate_command_policy(payload.command)
    if not is_valid:
        raise HTTPException(
            status_code=400,
            detail={"code": "COMMAND_POLICY_VIOLATION", "message": reason}
        )

    loop = asyncio.get_running_loop()
    start_t = loop.time()
    command = await asyncio.to_thread(fleet.submit, payload.hostname, payload.command, principal.subject, payload.timeout)
    answered = await fleet.wait(command.id, payload.timeout)
    duration_ms = round((loop.time() - start_t) * 1000, 1)
    if answered is None or answered.status not in ("completed", "failed"):
        raise HTTPException(
            status_code=504,
            detail={
                "code": "AGENT_COMMAND_TIMEOUT",
                "message": f"Command execution timed out after {payload.timeout}s "
                           f"(status {answered.status if answered else 'unknown'}; agent held by {connection['replicaId']})",
            }
        )
    result = answered.result or {}
    output_str = str(result.get("output", ""))

    # Append to Local Disk Tamper-Evident Audit Ledger
    try:
        from .audit_ledger import append_audit_entry
        append_audit_entry(
            action="agent.command_execute",
            actor=principal.subject,
            correlation_id=str(command.id),
            payload={
                "hostname": payload.hostname,
                "command": payload.command,
                "exitCode": result.get("exitCode", 0),
                "durationMs": duration_ms,
                "claimedBy": answered.claimed_by,
            }
        )
    except Exception:
        # The ledger is supplementary -- `audit_events` in PostgreSQL is the record of
        # authority -- so a failure here must not fail the command. It must still be
        # visible: a ledger that quietly stopped recording is worth nothing at the
        # moment someone goes looking.
        logger.warning("audit ledger append failed for agent command %s", command.id, exc_info=True)

    return {
        "taskId": str(command.id),
        "hostname": payload.hostname,
        "command": payload.command,
        "exitCode": result.get("exitCode", 0),
        "output": output_str,
        "stdout": output_str,
        "stderr": "",
        "durationMs": duration_ms,
        "requestedOn": fleet.replica_id,
        "claimedBy": answered.claimed_by,
    }


@app.get("/api/v1/agents/install.sh", response_class=Response)
def get_agent_install_script() -> Response:
    script = """#!/usr/bin/env bash
# netCI Edge Runner Agent Installer
set -e
SERVER_URL="${NETCI_SERVER_URL:-http://127.0.0.1:8100}"
AGENT_ID="${NETCI_AGENT_ID:-runner-$(hostname)}"
echo "[netCI] Installing Edge Runner Agent for host: $(hostname)..."
python3 -m pip install websockets || pip install websockets
echo "[netCI] Agent daemon ready. Launching outbound connection to ${SERVER_URL}..."
exec python3 -m backend.app.adapters.agent_daemon --server "${SERVER_URL}" --agent-id "${AGENT_ID}" --hostname "$(hostname)"
"""
    return Response(content=script, media_type="text/x-shellscript")


@app.get("/api/v1/security/waivers")
def list_security_waivers(
    moduleId: str | None = None,
    activeOnly: bool = True,
    principal: Principal = ReadAccess,
) -> list[dict[str, object]]:
    with database.transaction() as session:
        waivers = session.security_waivers(module_id=moduleId, active_only=activeOnly)
    return [
        {
            "id": str(w.id),
            "cveId": w.cve_id,
            "moduleId": w.module_id,
            "reason": w.reason,
            "approvedBy": w.approved_by,
            "status": w.status.value,
            "expiresAt": w.expires_at.isoformat(),
            "createdAt": w.created_at.isoformat(),
            "isValid": w.is_valid,
        }
        for w in waivers
    ]


@app.post("/api/v1/security/waivers", status_code=status.HTTP_201_CREATED)
def create_security_waiver(
    payload: SecurityWaiverCreate,
    principal: Principal = ReviewerAccess,
) -> dict[str, object]:
    now = datetime.now(timezone.utc)
    if payload.expiresAt <= now:
        raise HTTPException(
            status_code=422,
            detail={"code": "EXPIRED_DATE", "message": "Security waiver expiresAt must be in the future"},
        )
    waiver = SecurityWaiver(
        id=uuid4(),
        cve_id=payload.cveId.upper(),
        module_id=payload.moduleId,
        reason=payload.reason,
        approved_by=principal.subject,
        status=WaiverStatus.ACTIVE,
        expires_at=payload.expiresAt,
        created_at=now,
    )
    with database.transaction() as session:
        session.insert_security_waiver(waiver)
    return {
        "id": str(waiver.id),
        "cveId": waiver.cve_id,
        "moduleId": waiver.module_id,
        "reason": waiver.reason,
        "approvedBy": waiver.approved_by,
        "status": waiver.status.value,
        "expiresAt": waiver.expires_at.isoformat(),
        "createdAt": waiver.created_at.isoformat(),
        "isValid": waiver.is_valid,
    }


@app.post("/api/v1/security/waivers/{waiverId}/revoke", status_code=status.HTTP_200_OK)
def revoke_security_waiver(
    waiverId: UUID,
    principal: Principal = ReviewerAccess,
) -> dict[str, object]:
    with database.transaction() as session:
        success = session.revoke_security_waiver(waiverId)
    if not success:
        raise HTTPException(
            status_code=404,
            detail={"code": "WAIVER_NOT_FOUND", "message": f"Active security waiver {waiverId} not found"},
        )
    return {"id": str(waiverId), "status": "revoked"}


@app.post("/api/v1/servers/{server_name}/maintenance", status_code=status.HTTP_200_OK)
def update_server_maintenance(
    server_name: str,
    payload: ServerMaintenanceRequest,
    principal: Principal = AdminAccess,
) -> dict[str, object]:
    # Maintenance mode decides where deployments may go, so it is platform-admin only.
    state = ServerMaintenanceState(
        server_name=server_name,
        in_maintenance=payload.inMaintenance,
        reason=payload.reason,
        updated_by=principal.subject,
        updated_at=datetime.now(timezone.utc),
    )
    with database.transaction() as session:
        session.upsert_server_maintenance(state)
    return {
        "serverName": state.server_name,
        "inMaintenance": state.in_maintenance,
        "reason": state.reason,
        "updatedBy": state.updated_by,
        "updatedAt": state.updated_at.isoformat(),
    }


@app.get("/api/v1/servers/maintenance")
def list_servers_maintenance(
    principal: Principal = ReadAccess,
) -> list[dict[str, object]]:
    with database.transaction() as session:
        records = session.list_server_maintenance()
    return [
        {
            "serverName": r.server_name,
            "inMaintenance": r.in_maintenance,
            "reason": r.reason,
            "updatedBy": r.updated_by,
            "updatedAt": r.updated_at.isoformat(),
        }
        for r in records
    ]


@app.get("/api/v1/servers/{server_name}/telemetry")
def get_server_telemetry(
    server_name: str,
    principal: Principal = ReadAccess,
) -> dict[str, object]:
    telem = stored_server_telemetry(server_name)
    now = datetime.now(timezone.utc)
    if not telem:
        # No agent has reported for this server. Answering with invented "normal"
        # numbers -- which this endpoint used to do -- is a fabricated green.
        raise HTTPException(
            status_code=404,
            detail={"code": "TELEMETRY_UNKNOWN", "message": f"no telemetry has been reported for {server_name}"},
        )

    age_seconds = (now - telem.observed_at).total_seconds()
    is_stale = age_seconds > 300.0

    if is_stale:
        telemetry_status = "stale"
    elif telem.disk_percent > 90.0 or telem.cpu_percent > 95.0:
        telemetry_status = "critical"
    else:
        telemetry_status = "normal"

    return {
        "serverName": server_name,
        "cpuPercent": telem.cpu_percent,
        "memPercent": telem.mem_percent,
        "diskPercent": telem.disk_percent,
        "status": telemetry_status,
        "isStale": is_stale,
        "ageSeconds": round(age_seconds, 1),
        "observedAt": telem.observed_at.isoformat(),
    }


@app.post("/break-glass/requests", status_code=status.HTTP_201_CREATED)
def create_break_glass_request(
    payload: BreakGlassCreate,
    principal: Principal = DeveloperAccess,
) -> dict[str, object]:
    with database.transaction() as session:
        try:
            record = BreakGlassService.create_request(
                session,
                target_type=payload.targetType,
                target_id=payload.targetId,
                requested_by=principal.subject,
                reason=payload.reason,
                incident_ticket=payload.incidentTicket,
            )
        except BreakGlassError as exc:
            raise HTTPException(status_code=400, detail={"code": "BREAK_GLASS_INVALID", "message": str(exc)}) from exc
    return break_glass_json(record)


@app.post("/break-glass/requests/{requestId}/approve", status_code=status.HTTP_200_OK)
def approve_break_glass_request(
    requestId: UUID,
    payload: BreakGlassApprove | None = None,
    principal: Principal = ReviewerAccess,
) -> dict[str, object]:
    ttl = payload.ttlMinutes if payload else 60
    with database.transaction() as session:
        try:
            record = BreakGlassService.approve_request(
                session,
                request_id=requestId,
                approved_by=principal.subject,
                ttl_minutes=ttl,
            )
        except BreakGlassError as exc:
            msg = str(exc)
            if "dual-control" in msg.lower():
                raise HTTPException(
                    status_code=403, detail={"code": "SEPARATION_OF_DUTIES", "message": msg}
                ) from exc
            if "not found" in msg.lower():
                raise HTTPException(
                    status_code=404, detail={"code": "REQUEST_NOT_FOUND", "message": msg}
                ) from exc
            raise HTTPException(
                status_code=409, detail={"code": "BREAK_GLASS_STATE_INVALID", "message": msg}
            ) from exc
    return break_glass_json(record)


@app.get("/break-glass/active")
def get_active_break_glass(
    targetType: str,
    targetId: str,
    principal: Principal = ReadAccess,
) -> dict[str, object]:
    with database.transaction() as session:
        record = BreakGlassService.active_break_glass(session, target_type=targetType, target_id=targetId)
    if not record:
        raise HTTPException(
            status_code=404,
            detail={"code": "BREAK_GLASS_NOT_ACTIVE", "message": f"No active break-glass for {targetType}:{targetId}"},
        )
    return break_glass_json(record)


@app.get("/quotas/{scope}/{scopeId}")
def get_resource_quota(
    scope: str,
    scopeId: str,
    principal: Principal = ReadAccess,
) -> dict[str, object]:
    with database.transaction() as session:
        record = session.get_resource_quota(scope, scopeId)
        if not record:
            record = ResourceQuotaRecord(
                id=uuid4(),
                scope=scope,
                scope_id=scopeId,
                max_concurrent_pipelines=5,
                max_concurrent_deployments=2,
                max_production_requests_per_day=20,
            )
    return resource_quota_json(record)


@app.put("/quotas/{scope}/{scopeId}", status_code=status.HTTP_200_OK)
def set_resource_quota(
    scope: str,
    scopeId: str,
    payload: ResourceQuotaUpdate,
    principal: Principal = AdminAccess,
) -> dict[str, object]:
    record = ResourceQuotaRecord(
        id=uuid4(),
        scope=scope,
        scope_id=scopeId,
        max_concurrent_pipelines=payload.maxConcurrentPipelines,
        max_concurrent_deployments=payload.maxConcurrentDeployments,
        max_production_requests_per_day=payload.maxProductionRequestsPerDay,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    with database.transaction() as session:
        session.set_resource_quota(record)
    return resource_quota_json(record)


@app.post("/admission/validate", status_code=status.HTTP_200_OK)
def validate_kubernetes_admission(
    payload: dict[str, Any],
) -> dict[str, Any]:
    with database.transaction() as session:
        return AdmissionController.handle_admission_review(session, payload)


# ------------------------------------------------------------- Phase 12: Catalog & Self-Service

class CatalogServiceCreate(StrictBody):
    serviceId: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=255)
    description: str = Field(default="", max_length=2000)
    owningTeam: str = Field(min_length=1, max_length=128)
    tier: str = Field(default="tier-2", max_length=32)
    lifecycle: str = Field(default="active", max_length=32)
    repoUrl: str = Field(default="", max_length=1000)
    docsUrl: str = Field(default="", max_length=1000)
    metadata: dict[str, Any] = Field(default_factory=dict)


class CatalogServiceUpdate(StrictBody):
    name: str | None = Field(default=None, max_length=255)
    description: str | None = Field(default=None, max_length=2000)
    owningTeam: str | None = Field(default=None, max_length=128)
    tier: str | None = Field(default=None, max_length=32)
    lifecycle: str | None = Field(default=None, max_length=32)
    repoUrl: str | None = Field(default=None, max_length=1000)
    docsUrl: str | None = Field(default=None, max_length=1000)
    metadata: dict[str, Any] | None = None


class ServiceDependencyCreate(StrictBody):
    targetServiceId: str = Field(min_length=1, max_length=128)
    dependencyType: str = Field(default="sync", max_length=32)
    description: str = Field(default="", max_length=1000)


class CatalogTemplateCreate(StrictBody):
    templateId: str = Field(min_length=1, max_length=128)
    version: str = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=255)
    description: str = Field(default="", max_length=2000)
    category: str = Field(default="backend", max_length=64)
    parametersSchema: dict[str, Any] = Field(default_factory=dict)
    pipelineDefinition: dict[str, Any] = Field(default_factory=dict)
    isDeprecated: bool = False


class TemplateInstantiateRequest(StrictBody):
    version: str | None = Field(default=None, max_length=32)
    applicationName: str = Field(min_length=1, max_length=63)
    owningTeam: str = Field(min_length=1, max_length=128)
    parameters: dict[str, Any] = Field(default_factory=dict)


class PreviewEnvironmentCreate(StrictBody):
    applicationId: UUID
    pullRequestId: str = Field(min_length=1, max_length=64)
    commitSha: str = Field(min_length=1, max_length=64)
    ttlSeconds: int = Field(default=86400, ge=3600, le=259200)
    createdBy: str | None = Field(default=None, max_length=128)


class ResourceRequestCreate(StrictBody):
    applicationId: UUID
    teamId: str = Field(min_length=1, max_length=128)
    environment: str = Field(default="preview", max_length=32)
    resourceType: str = Field(min_length=1, max_length=64)
    spec: dict[str, Any] = Field(default_factory=dict)
    requestedBy: str | None = Field(default=None, max_length=128)



def catalog_service_json(rec: CatalogServiceRecord) -> dict[str, Any]:
    return {
        "serviceId": rec.id,
        "name": rec.name,
        "description": rec.description,
        "owningTeam": rec.owning_team,
        "tier": rec.tier,
        "lifecycle": rec.lifecycle,
        "repoUrl": rec.repo_url,
        "docsUrl": rec.docs_url,
        "metadata": rec.metadata,
        "createdAt": rec.created_at.isoformat(),
        "updatedAt": rec.updated_at.isoformat(),
    }


def catalog_template_json(rec: CatalogTemplateRecord) -> dict[str, Any]:
    return {
        "templateId": rec.id,
        "version": rec.version,
        "name": rec.name,
        "description": rec.description,
        "category": rec.category,
        "parametersSchema": rec.parameters_schema,
        "pipelineDefinition": rec.pipeline_definition,
        "isDeprecated": rec.is_deprecated,
        "createdAt": rec.created_at.isoformat(),
        "updatedAt": rec.updated_at.isoformat(),
    }


def preview_environment_json(rec: PreviewEnvironmentRecord) -> dict[str, Any]:
    return {
        "previewId": rec.id,
        "applicationId": str(rec.application_id),
        "pullRequestId": rec.pull_request_id,
        "commitSha": rec.commit_sha,
        "namespace": rec.namespace,
        "url": rec.url,
        "status": rec.status,
        "ttlSeconds": rec.ttl_seconds,
        "expiresAt": rec.expires_at.isoformat(),
        "createdBy": rec.created_by,
        "createdAt": rec.created_at.isoformat(),
        "destroyedAt": rec.destroyed_at.isoformat() if rec.destroyed_at else None,
    }


def resource_request_json(rec: ResourceRequestRecord) -> dict[str, Any]:
    return {
        "requestId": str(rec.id),
        "applicationId": str(rec.application_id),
        "teamId": rec.team_id,
        "environment": rec.environment,
        "resourceType": rec.resource_type,
        "spec": rec.spec,
        "status": rec.status,
        "statusReason": rec.status_reason,
        "provider": rec.provider,
        "outputs": rec.outputs,
        "requestedBy": rec.requested_by,
        "approvedBy": rec.approved_by,
        "createdAt": rec.created_at.isoformat(),
        "updatedAt": rec.updated_at.isoformat(),
    }


# --- Catalog Services Endpoints ---

@app.post("/catalog/services", status_code=status.HTTP_201_CREATED)
def register_catalog_service(
    payload: CatalogServiceCreate,
    principal: Principal = DeveloperAccess,
) -> dict[str, Any]:
    with database.transaction() as session:
        mgr = CatalogServiceManager(session)
        try:
            record = mgr.register_service(
                service_id=payload.serviceId,
                name=payload.name,
                owning_team=payload.owningTeam,
                description=payload.description,
                tier=payload.tier,
                lifecycle=payload.lifecycle,
                repo_url=payload.repoUrl,
                docs_url=payload.docsUrl,
                metadata=payload.metadata,
            )
        except CatalogValidationError as exc:
            raise HTTPException(status_code=400, detail={"code": "CATALOG_VALIDATION_ERROR", "message": str(exc)}) from exc
    return catalog_service_json(record)


@app.get("/catalog/services")
def list_catalog_services(
    owningTeam: str | None = None,
    tier: str | None = None,
    lifecycle: str | None = None,
    limit: int = 50,
    cursor: str | None = None,
    principal: Principal = ReadAccess,
) -> dict[str, Any]:
    with database.transaction() as session:
        items, next_cursor, has_more = session.list_catalog_services(
            owning_team=owningTeam,
            tier=tier,
            lifecycle=lifecycle,
            limit=limit,
            cursor=cursor,
        )
    return {
        "items": [catalog_service_json(s) for s in items],
        "nextCursor": next_cursor,
        "hasMore": has_more,
    }


@app.get("/catalog/services/{serviceId}")
def get_catalog_service(
    serviceId: str,
    principal: Principal = ReadAccess,
) -> dict[str, Any]:
    with database.transaction() as session:
        record = session.catalog_service(serviceId)
    if not record:
        raise HTTPException(status_code=404, detail={"code": "SERVICE_NOT_FOUND", "message": f"Service '{serviceId}' not found"})
    return catalog_service_json(record)


@app.put("/catalog/services/{serviceId}")
def update_catalog_service(
    serviceId: str,
    payload: CatalogServiceUpdate,
    principal: Principal = DeveloperAccess,
) -> dict[str, Any]:
    with database.transaction() as session:
        mgr = CatalogServiceManager(session)
        try:
            record = mgr.update_service(
                service_id=serviceId,
                name=payload.name,
                description=payload.description,
                owning_team=payload.owningTeam,
                tier=payload.tier,
                lifecycle=payload.lifecycle,
                repo_url=payload.repoUrl,
                docs_url=payload.docsUrl,
                metadata=payload.metadata,
            )
        except CatalogValidationError as exc:
            raise HTTPException(status_code=400, detail={"code": "CATALOG_VALIDATION_ERROR", "message": str(exc)}) from exc
    return catalog_service_json(record)


@app.get("/catalog/services/{serviceId}/dependencies")
def get_service_dependencies(
    serviceId: str,
    principal: Principal = ReadAccess,
) -> dict[str, Any]:
    with database.transaction() as session:
        mgr = CatalogServiceManager(session)
        try:
            graph = mgr.get_dependency_graph(serviceId)
        except CatalogValidationError as exc:
            raise HTTPException(status_code=404, detail={"code": "SERVICE_NOT_FOUND", "message": str(exc)}) from exc
    return {
        "serviceId": graph.service_id,
        "nodes": [catalog_service_json(n) for n in graph.nodes],
        "edges": graph.edges,
        "upstream": graph.upstream,
        "downstream": graph.downstream,
        "hasCycle": graph.has_cycle,
        "cycles": graph.cycles,
    }


@app.post("/catalog/services/{serviceId}/dependencies", status_code=status.HTTP_201_CREATED)
def add_service_dependency(
    serviceId: str,
    payload: ServiceDependencyCreate,
    principal: Principal = DeveloperAccess,
) -> dict[str, Any]:
    with database.transaction() as session:
        mgr = CatalogServiceManager(session)
        try:
            dep = mgr.add_dependency(
                source_service_id=serviceId,
                target_service_id=payload.targetServiceId,
                dependency_type=payload.dependencyType,
                description=payload.description,
            )
        except CatalogValidationError as exc:
            raise HTTPException(status_code=400, detail={"code": "DEPENDENCY_ERROR", "message": str(exc)}) from exc
    return {
        "dependencyId": str(dep.id),
        "sourceServiceId": dep.source_service_id,
        "targetServiceId": dep.target_service_id,
        "dependencyType": dep.dependency_type,
        "description": dep.description,
        "createdAt": dep.created_at.isoformat(),
    }


@app.delete("/catalog/services/{serviceId}/dependencies/{targetServiceId}", status_code=status.HTTP_204_NO_CONTENT)
def remove_service_dependency(
    serviceId: str,
    targetServiceId: str,
    principal: Principal = DeveloperAccess,
) -> None:
    with database.transaction() as session:
        mgr = CatalogServiceManager(session)
        deleted = mgr.remove_dependency(serviceId, targetServiceId)
    if not deleted:
        raise HTTPException(status_code=404, detail={"code": "DEPENDENCY_NOT_FOUND", "message": "dependency does not exist"})


# --- Catalog Templates Endpoints ---

@app.post("/catalog/templates", status_code=status.HTTP_201_CREATED)
def register_catalog_template(
    payload: CatalogTemplateCreate,
    principal: Principal = AdminAccess,
) -> dict[str, Any]:
    with database.transaction() as session:
        engine = PipelineTemplateEngine(session)
        try:
            record = engine.register_template(
                template_id=payload.templateId,
                version=payload.version,
                name=payload.name,
                description=payload.description,
                category=payload.category,
                parameters_schema=payload.parametersSchema,
                pipeline_definition=payload.pipelineDefinition,
                is_deprecated=payload.isDeprecated,
            )
        except TemplateVersionConflict as exc:
            raise HTTPException(status_code=409, detail={"code": "TEMPLATE_VERSION_EXISTS", "message": str(exc)}) from exc
        except TemplateValidationError as exc:
            raise HTTPException(status_code=400, detail={"code": "TEMPLATE_VALIDATION_ERROR", "message": str(exc)}) from exc
    return catalog_template_json(record)


@app.get("/catalog/templates")
def list_catalog_templates(
    category: str | None = None,
    includeDeprecated: bool = False,
    principal: Principal = ReadAccess,
) -> dict[str, Any]:
    with database.transaction() as session:
        templates = session.list_catalog_templates(category=category, include_deprecated=includeDeprecated)
        if not templates and not category:
            seed_builtin_templates(session)
            templates = session.list_catalog_templates(category=category, include_deprecated=includeDeprecated)
    return {
        "items": [catalog_template_json(t) for t in templates],
    }


@app.get("/catalog/templates/{templateId}")
def get_catalog_template(
    templateId: str,
    version: str | None = None,
    principal: Principal = ReadAccess,
) -> dict[str, Any]:
    with database.transaction() as session:
        record = session.catalog_template(templateId, version=version)
        if not record and not version:
            seed_builtin_templates(session)
            record = session.catalog_template(templateId)
    if not record:
        ver_msg = f" (version {version})" if version else ""
        raise HTTPException(status_code=404, detail={"code": "TEMPLATE_NOT_FOUND", "message": f"Template '{templateId}'{ver_msg} not found"})
    return catalog_template_json(record)


@app.post("/catalog/templates/{templateId}/instantiate")
def instantiate_catalog_template(
    templateId: str,
    payload: TemplateInstantiateRequest,
    principal: Principal = DeveloperAccess,
) -> dict[str, Any]:
    with database.transaction() as session:
        engine = PipelineTemplateEngine(session)
        try:
            inst = engine.instantiate(
                template_id=templateId,
                version=payload.version,
                application_name=payload.applicationName,
                owning_team=payload.owningTeam,
                parameters=payload.parameters,
            )
        except TemplateValidationError as exc:
            raise HTTPException(status_code=400, detail={"code": "INSTANTIATION_ERROR", "message": str(exc)}) from exc
    return {
        "templateId": inst.template_id,
        "version": inst.version,
        "applicationName": inst.application_name,
        "owningTeam": inst.owning_team,
        "runtime": inst.runtime,
        "stages": inst.stages,
        "pipelineConfig": inst.pipeline_config,
        "deploymentConfig": inst.deployment_config,
    }


# --- Preview Environments Endpoints ---

@app.post("/preview-environments", status_code=status.HTTP_201_CREATED)
def create_preview_environment(
    payload: PreviewEnvironmentCreate,
    principal: Principal = DeveloperAccess,
) -> dict[str, Any]:
    with database.transaction() as session:
        mgr = PreviewEnvironmentManager(session)
        try:
            record = mgr.create_preview(
                application_id=payload.applicationId,
                pull_request_id=payload.pullRequestId,
                commit_sha=payload.commitSha,
                ttl_seconds=payload.ttlSeconds,
                created_by=payload.createdBy or principal.subject,
            )
        except PreviewEnvironmentError as exc:
            raise HTTPException(status_code=400, detail={"code": "PREVIEW_ERROR", "message": str(exc)}) from exc
    return preview_environment_json(record)


@app.get("/preview-environments")
def list_preview_environments(
    applicationId: UUID | None = None,
    status: str | None = None,
    principal: Principal = ReadAccess,
) -> dict[str, Any]:
    with database.transaction() as session:
        items = session.list_preview_environments(application_id=applicationId, status=status)
    return {
        "items": [preview_environment_json(p) for p in items],
    }


@app.get("/preview-environments/{previewId}")
def get_preview_environment(
    previewId: str,
    principal: Principal = ReadAccess,
) -> dict[str, Any]:
    with database.transaction() as session:
        record = session.preview_environment(previewId)
    if not record:
        raise HTTPException(status_code=404, detail={"code": "PREVIEW_NOT_FOUND", "message": f"Preview environment '{previewId}' not found"})
    return preview_environment_json(record)


@app.post("/preview-environments/{previewId}/teardown")
def teardown_preview_environment(
    previewId: str,
    principal: Principal = DeveloperAccess,
) -> dict[str, Any]:
    with database.transaction() as session:
        mgr = PreviewEnvironmentManager(session)
        try:
            record = mgr.teardown_preview(previewId)
        except PreviewEnvironmentError as exc:
            raise HTTPException(status_code=404, detail={"code": "PREVIEW_NOT_FOUND", "message": str(exc)}) from exc
    return preview_environment_json(record)


@app.post("/preview-environments/reconcile-expiry")
def reconcile_preview_environments_expiry(
    principal: Principal = AdminAccess,
) -> dict[str, Any]:
    with database.transaction() as session:
        mgr = PreviewEnvironmentManager(session)
        expired = mgr.reconcile_expiry()
    return {
        "reconciledCount": len(expired),
        "items": [preview_environment_json(p) for p in expired],
    }


# --- Self-Service Resources Endpoints ---

@app.post("/self-service/resources", status_code=status.HTTP_201_CREATED)
def request_self_service_resource(
    payload: ResourceRequestCreate,
    principal: Principal = DeveloperAccess,
) -> dict[str, Any]:
    with database.transaction() as session:
        mgr = SelfServiceResourceManager(session)
        try:
            record = mgr.request_resource(
                application_id=payload.applicationId,
                team_id=payload.teamId,
                environment=payload.environment,
                resource_type=payload.resourceType,
                spec=payload.spec,
                requested_by=payload.requestedBy or principal.subject,
            )
        except ResourceRequestError as exc:
            raise HTTPException(status_code=400, detail={"code": "RESOURCE_REQUEST_ERROR", "message": str(exc)}) from exc
    return resource_request_json(record)


@app.get("/self-service/resources")
def list_self_service_resources(
    applicationId: UUID | None = None,
    teamId: str | None = None,
    environment: str | None = None,
    status: str | None = None,
    limit: int = 50,
    cursor: str | None = None,
    principal: Principal = ReadAccess,
) -> dict[str, Any]:
    with database.transaction() as session:
        items, next_cursor, has_more = session.list_resource_requests(
            application_id=applicationId,
            team_id=teamId,
            environment=environment,
            status=status,
            limit=limit,
            cursor=cursor,
        )
    return {
        "items": [resource_request_json(r) for r in items],
        "nextCursor": next_cursor,
        "hasMore": has_more,
    }


@app.get("/self-service/resources/{requestId}")
def get_self_service_resource(
    requestId: UUID,
    principal: Principal = ReadAccess,
) -> dict[str, Any]:
    with database.transaction() as session:
        record = session.resource_request(requestId)
    if not record:
        raise HTTPException(status_code=404, detail={"code": "RESOURCE_NOT_FOUND", "message": f"Resource request '{requestId}' not found"})
    return resource_request_json(record)


class ResourceApproveRequest(StrictBody):
    approvedBy: str | None = None


@app.post("/self-service/resources/{requestId}/approve")
def approve_self_service_resource(
    requestId: UUID,
    payload: ResourceApproveRequest | None = None,
    principal: Principal = ReviewerAccess,
) -> dict[str, Any]:
    approver = payload.approvedBy if (payload and payload.approvedBy) else principal.subject
    sod = separation_of_duties_enabled(principal) or bool(payload and payload.approvedBy)
    with database.transaction() as session:
        mgr = SelfServiceResourceManager(session)
        try:
            record = mgr.approve_resource(requestId, approved_by=approver, enforce_sod=sod)
        except ResourceRequestError as exc:
            msg = str(exc)
            if "separation of duties" in msg.lower():
                raise HTTPException(status_code=403, detail={"code": "SEPARATION_OF_DUTIES", "message": msg}) from exc
            if "not found" in msg.lower():
                raise HTTPException(status_code=404, detail={"code": "RESOURCE_NOT_FOUND", "message": msg}) from exc
            raise HTTPException(status_code=400, detail={"code": "RESOURCE_APPROVE_ERROR", "message": msg}) from exc
    return resource_request_json(record)


@app.post("/self-service/resources/{requestId}/deprovision")
def deprovision_self_service_resource(
    requestId: UUID,
    principal: Principal = DeveloperAccess,
) -> dict[str, Any]:
    with database.transaction() as session:
        mgr = SelfServiceResourceManager(session)
        try:
            record = mgr.deprovision_resource(requestId)
        except ResourceRequestError as exc:
            raise HTTPException(status_code=404, detail={"code": "RESOURCE_NOT_FOUND", "message": str(exc)}) from exc
    return resource_request_json(record)
