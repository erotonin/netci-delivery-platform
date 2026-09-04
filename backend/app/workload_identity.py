"""Short-lived, scoped credentials for the machines that call netCI back.

A single shared `NETCI_PIPELINE_API_KEY` proved one thing only: that the caller had
netCI's pipeline secret. It could not say *which* build was calling, so any holder could
report a result for any run, on any application, and a Jenkins controller could report a
deployment outcome it had no part in. A secret that answers "are you the pipeline?" is
not an identity; it is a password shared by every workload in the estate.

A callback token here answers the question that actually matters -- *which* piece of work
is this, and what is it allowed to say?  Every token names:

* `iss`/`aud`  -- who minted it and which netCI it is for, so a token from a staging
                  installation is not accepted by production;
* `sub`        -- the workload kind (`jenkins`, `temporal`), so a CI controller cannot
                  report a deployment result;
* `application_id` plus exactly one of `pipeline_run_id` / `deployment_id` -- checked
                  against the route, so a token for run A cannot write to run B;
* `scopes`     -- the operations it may perform, checked per endpoint;
* `iat`/`exp`  -- minutes, not forever;
* `jti`        -- recorded on use, which is what makes a captured token single-use where
                  the operation is terminal.

Signing is HMAC-SHA256 over a compact JSON envelope. This is deliberately not a general
JWT library: netCI both mints and verifies these tokens, so the algorithm is fixed at the
verifier (no `alg` confusion), the claim set is closed, and there is nothing to negotiate.
Keys carry a `kid` and several may be configured at once, which is what makes rotation a
config change rather than an outage: publish the new key, let both verify, then retire the
old one.

`NETCI_WORKLOAD_TOKEN_KEYS` is `kid:secret` pairs, comma-separated; the first is active
for signing. `NETCI_WORKLOAD_TOKEN_KEYS_FILE` reads the same format from a mounted
secret, which is what a Kubernetes or Compose deployment should use. Neither the key nor
the token is ever logged or written to the database -- only the `jti`.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from dataclasses import dataclass
from uuid import UUID, uuid4

from .runtime_environment import is_local_runtime

logger = logging.getLogger(__name__)

DEFAULT_ISSUER = "netci"
DEFAULT_AUDIENCE = "netci-api"
#: Long enough for a slow build stage to finish and report, short enough that a token
#: scraped from a build log is worthless by the time anyone reads it.
DEFAULT_TTL_SECONDS = 3600
MAX_TTL_SECONDS = 86400


class WorkloadIdentityError(Exception):
    def __init__(self, code: str, message: str, status: int = 401) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


class Workload:
    """The machine kinds netCI issues credentials to."""

    JENKINS = "jenkins"
    TEMPORAL = "temporal"

    ALL = frozenset({JENKINS, TEMPORAL})


class Scope:
    """What a callback token may do. One scope per netCI operation, never a wildcard."""

    CI_RESULT = "ci:result"
    CI_LOGS = "ci:logs"
    CI_EVIDENCE = "ci:evidence"
    CI_REPORT = "ci:report"
    CI_STAGE = "ci:stage"
    DEPLOYMENT_RESULT = "deployment:result"
    DEPLOYMENT_READ = "deployment:read"

    ALL = frozenset(
        {CI_RESULT, CI_LOGS, CI_EVIDENCE, CI_REPORT, CI_STAGE, DEPLOYMENT_RESULT, DEPLOYMENT_READ}
    )


#: Which workload may hold which scope. This is the table that stops a Jenkins controller
#: from reporting a deployment outcome: the scope is not merely absent from its token, it
#: cannot be minted into one.
WORKLOAD_SCOPES: dict[str, frozenset[str]] = {
    Workload.JENKINS: frozenset(
        {Scope.CI_RESULT, Scope.CI_LOGS, Scope.CI_EVIDENCE, Scope.CI_REPORT, Scope.CI_STAGE}
    ),
    Workload.TEMPORAL: frozenset(
        {Scope.DEPLOYMENT_RESULT, Scope.DEPLOYMENT_READ, Scope.CI_EVIDENCE}
    ),
}

#: Operations that end a piece of work. A token for one of these is single-use, so a
#: token captured from a worker's environment cannot be replayed later to overwrite a
#: newer result.
TERMINAL_SCOPES = frozenset({Scope.DEPLOYMENT_RESULT})


@dataclass(frozen=True)
class SigningKey:
    kid: str
    secret: bytes


@dataclass(frozen=True)
class CallbackClaims:
    """A verified callback credential, reduced to what an endpoint has to check."""

    issuer: str
    audience: str
    workload: str
    application_id: UUID
    scopes: frozenset[str]
    issued_at: int
    expires_at: int
    jti: str
    pipeline_run_id: UUID | None = None
    deployment_id: UUID | None = None
    single_use: bool = False

    def permits(self, scope: str) -> bool:
        return scope in self.scopes

    def as_audit_payload(self) -> dict[str, object]:
        """What may be written to the audit trail. Never the token, never the signature."""

        return {
            "workload": self.workload,
            "applicationId": str(self.application_id),
            "pipelineRunId": str(self.pipeline_run_id) if self.pipeline_run_id else None,
            "deploymentId": str(self.deployment_id) if self.deployment_id else None,
            "scopes": sorted(self.scopes),
            "jti": self.jti,
            "expiresAt": self.expires_at,
        }


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _unb64url(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def configured_keys() -> list[SigningKey]:
    """Signing keys, newest first. Empty when workload identity is not configured."""

    raw = ""
    path = os.getenv("NETCI_WORKLOAD_TOKEN_KEYS_FILE", "").strip()
    if path:
        try:
            with open(path, encoding="utf-8") as handle:
                raw = handle.read().strip()
        except OSError as exc:
            # Deliberately not the path's contents, and not the exception's repr beyond
            # its message: this line goes to a log an operator may paste into a ticket.
            logger.error("NETCI_WORKLOAD_TOKEN_KEYS_FILE is unreadable: %s", exc.strerror)
            return []
    else:
        raw = os.getenv("NETCI_WORKLOAD_TOKEN_KEYS", "").strip()
    keys: list[SigningKey] = []
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry or ":" not in entry:
            continue
        kid, _, secret = entry.partition(":")
        kid, secret = kid.strip(), secret.strip()
        if not kid or len(secret) < 32:
            # A short secret is worse than none: it looks configured while being
            # brute-forceable, so it is refused rather than quietly accepted.
            logger.error("workload signing key %r is missing or shorter than 32 characters", kid)
            continue
        keys.append(SigningKey(kid=kid, secret=secret.encode("utf-8")))
    return keys


def workload_identity_configured() -> bool:
    return bool(configured_keys())


def issuer() -> str:
    return os.getenv("NETCI_WORKLOAD_TOKEN_ISSUER", DEFAULT_ISSUER).strip() or DEFAULT_ISSUER


def audience() -> str:
    return os.getenv("NETCI_WORKLOAD_TOKEN_AUDIENCE", DEFAULT_AUDIENCE).strip() or DEFAULT_AUDIENCE


def legacy_shared_key_allowed() -> bool:
    """Whether the shared pipeline key may still authenticate a callback.

    Only in local mode, or when an operator has explicitly opted into the legacy mode
    for a migration window. Outside those, a shared key is refused even if it is set --
    otherwise "we rolled out workload identity" and "the old key still works" are both
    true at once, and only the second one matters to an attacker.
    """

    if is_local_runtime():
        return True
    return os.getenv("NETCI_ALLOW_LEGACY_PIPELINE_KEY", "false").strip().lower() in {"1", "true", "yes"}


def require_configured_workload_identity() -> None:
    """Refuse to start without a safe way to authenticate machine callbacks."""

    if is_local_runtime():
        return
    if workload_identity_configured():
        return
    if legacy_shared_key_allowed():
        logger.warning(
            "NETCI_ALLOW_LEGACY_PIPELINE_KEY is set: machine callbacks authenticate with a "
            "shared key that is not bound to a run, a deployment or a workload. "
            "Configure NETCI_WORKLOAD_TOKEN_KEYS and remove this setting."
        )
        return
    raise RuntimeError(
        "NETCI_WORKLOAD_TOKEN_KEYS (or NETCI_WORKLOAD_TOKEN_KEYS_FILE) is required outside "
        "local mode: netCI will not accept machine callbacks authenticated by a shared key "
        "that cannot say which run is calling. Set NETCI_ALLOW_LEGACY_PIPELINE_KEY=true "
        "only for an explicit migration window."
    )


def mint(
    *,
    workload: str,
    application_id: UUID,
    scopes: set[str] | frozenset[str],
    pipeline_run_id: UUID | None = None,
    deployment_id: UUID | None = None,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    now: int | None = None,
) -> str:
    """Issue a callback token for one piece of work.

    Refuses to mint a token that is broader than the workload is allowed to be, so a bug
    in a caller cannot widen a Jenkins token into one that reports deployments.
    """

    keys = configured_keys()
    if not keys:
        raise WorkloadIdentityError(
            "WORKLOAD_IDENTITY_NOT_CONFIGURED",
            "no workload signing key is configured",
            503,
        )
    if workload not in Workload.ALL:
        raise WorkloadIdentityError("UNKNOWN_WORKLOAD", f"unknown workload {workload!r}", 422)
    requested = frozenset(scopes)
    unknown = requested - Scope.ALL
    if unknown:
        raise WorkloadIdentityError(
            "UNKNOWN_SCOPE", f"unknown scopes: {', '.join(sorted(unknown))}", 422
        )
    not_permitted = requested - WORKLOAD_SCOPES[workload]
    if not_permitted:
        raise WorkloadIdentityError(
            "SCOPE_NOT_PERMITTED",
            f"{workload} may not hold: {', '.join(sorted(not_permitted))}",
            422,
        )
    if (pipeline_run_id is None) == (deployment_id is None):
        raise WorkloadIdentityError(
            "AMBIGUOUS_SUBJECT",
            "a callback token names exactly one of pipeline_run_id or deployment_id",
            422,
        )
    ttl = max(1, min(int(ttl_seconds), MAX_TTL_SECONDS))
    issued = int(now if now is not None else time.time())
    claims = {
        "iss": issuer(),
        "aud": audience(),
        "sub": workload,
        "application_id": str(application_id),
        "pipeline_run_id": str(pipeline_run_id) if pipeline_run_id else None,
        "deployment_id": str(deployment_id) if deployment_id else None,
        "scopes": sorted(requested),
        "iat": issued,
        "exp": issued + ttl,
        "jti": uuid4().hex,
        "single_use": bool(requested & TERMINAL_SCOPES),
    }
    active = keys[0]
    header = {"typ": "netci-callback", "alg": "HS256", "kid": active.kid}
    header_part = _b64url(json.dumps(header, sort_keys=True, separators=(",", ":")).encode())
    claims_part = _b64url(json.dumps(claims, sort_keys=True, separators=(",", ":")).encode())
    signing_input = f"{header_part}.{claims_part}".encode("ascii")
    signature = hmac.new(active.secret, signing_input, hashlib.sha256).digest()
    return f"{header_part}.{claims_part}.{_b64url(signature)}"


def verify(token: str, *, now: int | None = None) -> CallbackClaims:
    """Verify a callback token, or raise. Never returns partially-checked claims."""

    keys = configured_keys()
    if not keys:
        raise WorkloadIdentityError(
            "WORKLOAD_IDENTITY_NOT_CONFIGURED", "no workload signing key is configured", 503
        )
    parts = token.split(".")
    if len(parts) != 3:
        raise WorkloadIdentityError("MALFORMED_TOKEN", "callback token is malformed")
    header_part, claims_part, signature_part = parts
    try:
        header = json.loads(_unb64url(header_part))
        claims = json.loads(_unb64url(claims_part))
        supplied_signature = _unb64url(signature_part)
    except (ValueError, json.JSONDecodeError) as exc:
        raise WorkloadIdentityError("MALFORMED_TOKEN", "callback token is malformed") from exc
    if not isinstance(header, dict) or not isinstance(claims, dict):
        raise WorkloadIdentityError("MALFORMED_TOKEN", "callback token is malformed")
    # The algorithm comes from the verifier, never from the token: an attacker who can
    # choose `alg` can choose `none`.
    if header.get("typ") != "netci-callback":
        raise WorkloadIdentityError("MALFORMED_TOKEN", "callback token is not a netCI callback token")

    signing_input = f"{header_part}.{claims_part}".encode("ascii")
    candidates = [key for key in keys if key.kid == header.get("kid")] or keys
    if not any(
        hmac.compare_digest(
            supplied_signature, hmac.new(key.secret, signing_input, hashlib.sha256).digest()
        )
        for key in candidates
    ):
        raise WorkloadIdentityError("INVALID_SIGNATURE", "callback token signature is not valid")

    if claims.get("iss") != issuer():
        raise WorkloadIdentityError("INVALID_ISSUER", "callback token was not issued by this netCI")
    if claims.get("aud") != audience():
        raise WorkloadIdentityError("INVALID_AUDIENCE", "callback token is for another audience")
    workload = claims.get("sub")
    if workload not in Workload.ALL:
        raise WorkloadIdentityError("UNKNOWN_WORKLOAD", "callback token names an unknown workload")

    moment = int(now if now is not None else time.time())
    try:
        expires_at = int(claims["exp"])
        issued_at = int(claims["iat"])
    except (KeyError, TypeError, ValueError) as exc:
        raise WorkloadIdentityError("MALFORMED_TOKEN", "callback token has no validity window") from exc
    if moment >= expires_at:
        raise WorkloadIdentityError("TOKEN_EXPIRED", "callback token has expired")
    if issued_at - 60 > moment:
        raise WorkloadIdentityError("TOKEN_NOT_YET_VALID", "callback token is not valid yet")

    raw_scopes = claims.get("scopes")
    if not isinstance(raw_scopes, list) or not all(isinstance(item, str) for item in raw_scopes):
        raise WorkloadIdentityError("MALFORMED_TOKEN", "callback token has no scopes")
    scopes = frozenset(raw_scopes) & Scope.ALL
    # A scope the workload may never hold is dropped even if it was signed, so a mis-issued
    # token cannot become an escalation.
    scopes &= WORKLOAD_SCOPES[workload]

    try:
        application_id = UUID(str(claims["application_id"]))
        pipeline_run_id = UUID(str(claims["pipeline_run_id"])) if claims.get("pipeline_run_id") else None
        deployment_id = UUID(str(claims["deployment_id"])) if claims.get("deployment_id") else None
    except (KeyError, ValueError) as exc:
        raise WorkloadIdentityError("MALFORMED_TOKEN", "callback token names no resource") from exc
    if (pipeline_run_id is None) == (deployment_id is None):
        raise WorkloadIdentityError(
            "MALFORMED_TOKEN", "callback token must name exactly one resource"
        )
    jti = str(claims.get("jti") or "")
    if not jti:
        raise WorkloadIdentityError("MALFORMED_TOKEN", "callback token has no jti")

    return CallbackClaims(
        issuer=str(claims["iss"]),
        audience=str(claims["aud"]),
        workload=workload,
        application_id=application_id,
        pipeline_run_id=pipeline_run_id,
        deployment_id=deployment_id,
        scopes=scopes,
        issued_at=issued_at,
        expires_at=expires_at,
        jti=jti,
        single_use=bool(claims.get("single_use")),
    )


def generate_key(kid: str = "k1") -> str:
    """A `kid:secret` pair an operator can paste into configuration."""

    return f"{kid}:{secrets.token_urlsafe(48)}"
