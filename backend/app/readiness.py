"""Readiness, circuit breakers, and truthful dependency health probing.

Readiness (/readyz) verifies whether the platform can actually accept and execute work,
failing closed (HTTP 503) if mandatory dependencies are unreachable or unconfigured.
Liveness (/livez) answers whether the process is alive.

Detailed operator health (/operator/health) provides authenticated diagnostic details
without leaking secrets or private filesystem paths.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def check_secrets() -> dict[str, Any]:
    """Verify presence and safe permissions of configured secret files without leaking secrets."""
    results: dict[str, Any] = {}

    token_file = os.getenv("NETCI_AUTH_TOKENS_FILE", "").strip()
    if token_file:
        p = Path(token_file)
        if not p.is_file():
            results["authTokensFile"] = {"status": "missing", "safe": False}
        else:
            try:
                mode = p.stat().st_mode
                world_writable = bool(mode & stat.S_IWOTH)
                results["authTokensFile"] = {
                    "status": "present",
                    "safe": not world_writable,
                    "warning": "world-writable" if world_writable else None,
                }
            except OSError as exc:
                results["authTokensFile"] = {"status": "error", "error": type(exc).__name__,
                                            "errorDetail": str(exc), "safe": False}
    else:
        results["authTokensFile"] = {"status": "not_configured", "safe": True}

    # Read through the same resolver the verifier uses, so a key mounted via
    # NETCI_WORKLOAD_TOKEN_KEYS_FILE is reported as configured rather than missing.
    from .workload_identity import configured_keys

    keys = configured_keys()
    results["workloadTokenKeys"] = {
        "status": "configured" if keys else "not_configured",
        "keyCount": len(keys),
        "activeKid": keys[0].kid if keys else None,
    }

    return results


def check_cosign() -> dict[str, Any]:
    """Check Cosign binary and verification key presence if signature verification is active."""
    mode = os.getenv("NETCI_SIGNATURE_VERIFY_MODE", "none").strip().lower()
    if mode != "cosign":
        return {"mode": mode, "status": "not_configured", "optional": True}

    # The same resolution the verifier uses (signature_verifier.build_signature_verifier),
    # so readiness answers for the binary that will actually run -- not for whichever
    # cosign happens to be first in PATH.
    executable = os.getenv("NETCI_COSIGN_EXECUTABLE", "cosign").strip() or "cosign"
    cosign_path = shutil.which(executable)
    if not cosign_path:
        return {
            "mode": "cosign",
            "status": "unavailable",
            "error": f"cosign executable not found: {executable}",
            "ready": False,
        }
    version = _cosign_version(cosign_path)

    key_file = os.getenv("NETCI_COSIGN_PUBLIC_KEY_FILE", "").strip()
    inline_key = os.getenv("NETCI_COSIGN_PUBLIC_KEY", "").strip()
    if key_file:
        p = Path(key_file)
        if not p.is_file():
            return {
                "mode": "cosign",
                "status": "unavailable",
                "error": "public key file missing",
                "ready": False,
            }
    elif not inline_key:
        return {
            "mode": "cosign",
            "status": "unavailable",
            "error": "no public key configured",
            "ready": False,
        }

    return {"mode": "cosign", "status": "ready", "ready": True, "executable": cosign_path, "version": version}


def _cosign_version(cosign_path: str) -> str | None:
    """Report which cosign verifies deployments.

    Cosign 3 stores image signatures as sigstore bundles under the OCI referrers scheme,
    which cosign 2 cannot read: verification fails with "no signatures found", which reads
    like a missing signature. Surfacing the version next to the CI toolbox's pin makes the
    mismatch visible before it blocks a deployment instead of after.
    """
    try:
        proc = subprocess.run(
            [cosign_path, "version", "--json"],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    try:
        return str(json.loads(proc.stdout).get("gitVersion") or "") or None
    except ValueError:
        return None


def check_ci(launcher: Any) -> dict[str, Any]:
    mode = getattr(launcher, "mode", "none")
    if mode == "jenkins":
        try:
            launcher.refresh_health()
            controllers = getattr(launcher.router, "controllers", [])
            healthy_count = sum(1 for c in controllers if getattr(c, "state", None) and c.state.value == "healthy")
            ready = healthy_count > 0
            # Where builds will run: a build the launcher cannot isolate is not
            # dispatched, so a provisioner that cannot reach its cluster makes CI not ready.
            provisioner = getattr(launcher, "isolation", None)
            isolation = provisioner.describe() if provisioner is not None else {"mode": "none", "ready": True}
            if not isolation.get("ready", True):
                ready = False
            return {
                "buildIsolation": isolation,
                "mode": "jenkins",
                "status": "ready" if ready else "unavailable",
                "healthyControllers": healthy_count,
                "totalControllers": len(controllers),
                "ready": ready,
            }
        except Exception as exc:
            return {"mode": "jenkins", "status": "unavailable", "error": type(exc).__name__,
                    "errorDetail": str(exc), "ready": False}
    return {"mode": mode, "status": "not_configured", "optional": True, "ready": True}


def check_cd(orchestrator: Any, *, timeout_seconds: float = 3.0) -> dict[str, Any]:
    """Whether the CD engine can actually take a deployment right now.

    "configured" used to be reported as ready. Configuration says an address was
    written down; it does not say the server answers, nor that any worker is polling
    the task queue. A deployment started against a queue nobody polls sits forever, and
    readiness that cannot see that is the false green this endpoint exists to refuse.
    """

    mode = getattr(orchestrator, "mode", "none")
    if mode != "temporal":
        return {"mode": mode, "status": "not_configured", "optional": True, "ready": True}
    address = getattr(orchestrator, "address", "unknown")
    namespace = getattr(orchestrator, "namespace", "default")
    task_queue = getattr(orchestrator, "task_queue", "netci-delivery")
    base = {"mode": "temporal", "address": address, "namespace": namespace, "taskQueue": task_queue}
    try:
        import asyncio

        async def probe() -> dict[str, Any]:
            from temporalio.client import Client
            from temporalio.api.enums.v1 import TaskQueueType
            from temporalio.api.workflowservice.v1 import DescribeTaskQueueRequest
            from temporalio.api.taskqueue.v1 import TaskQueue

            client = await asyncio.wait_for(Client.connect(address, namespace=namespace), timeout_seconds)
            response = await asyncio.wait_for(
                client.workflow_service.describe_task_queue(
                    DescribeTaskQueueRequest(
                        namespace=namespace,
                        task_queue=TaskQueue(name=task_queue),
                        task_queue_type=TaskQueueType.TASK_QUEUE_TYPE_WORKFLOW,
                    )
                ),
                timeout_seconds,
            )
            pollers = list(getattr(response, "pollers", []))
            now = datetime.now(timezone.utc)
            ages = []
            for poller in pollers:
                seen = getattr(poller, "last_access_time", None)
                if seen is not None and hasattr(seen, "ToDatetime"):
                    ages.append((now - seen.ToDatetime(tzinfo=timezone.utc)).total_seconds())
            return {"pollers": len(pollers), "youngestPollSecondsAgo": min(ages) if ages else None}

        result = asyncio.run(probe())
    except Exception as exc:  # noqa: BLE001 - the failure is the finding
        return {**base, "status": "unreachable", "error": type(exc).__name__, "ready": False}
    return judge_cd_pollers(base, result["pollers"], result["youngestPollSecondsAgo"])


# A worker long-polls the task queue roughly once a minute; a poller older than this
# has stopped asking for work.
CD_POLLER_MAX_AGE_SECONDS = 75.0


def judge_cd_pollers(base: dict[str, Any], pollers: int, youngest_age: float | None) -> dict[str, Any]:
    """Temporal keeps a poller in the list for about a minute after its last long-poll,
    so a worker that died seconds ago still appears. The age is reported so nobody
    reads "1 poller" as "1 live worker", and anything older than a poll cycle is dead."""

    report = {**base, "pollers": pollers, "youngestPollSecondsAgo": youngest_age}
    if pollers == 0 or (youngest_age is not None and youngest_age > CD_POLLER_MAX_AGE_SECONDS):
        return {**report, "status": "no_workers", "ready": False}
    return {**report, "status": "ready", "ready": True}


def check_dcim(portal: Any) -> dict[str, Any]:
    catalog = getattr(portal, "dcim_catalog", None)
    source = getattr(catalog, "source", None) if hasattr(catalog, "source") else None
    if source == "dcim-http" or hasattr(catalog, "base_url"):
        probe = getattr(catalog, "search_services", None)
        if probe:
            try:
                page = probe("")
                return {"status": page.status, "source": page.source, "ready": page.status == "ready"}
            except Exception as exc:
                return {"status": "unavailable", "error": type(exc).__name__,
                        "errorDetail": str(exc), "ready": False}
    return {"status": "not_configured", "optional": True, "ready": True}


def probe_readiness(platform: Any, portal: Any, authenticator: Any) -> tuple[bool, dict[str, Any]]:
    """Probe all dependencies and decide overall readiness."""
    portal_health = portal.persistence_health()
    delivery_health = platform.persistence_health()
    
    db_ok = not (delivery_health.startswith("unavailable") or portal_health.get("status") == "degraded")
    ci_health = check_ci(platform.ci_launcher)
    cd_health = check_cd(platform.cd_orchestrator)
    dcim_health = check_dcim(portal)
    cosign_health = check_cosign()
    secrets_health = check_secrets()
    traffic_health = check_traffic_router()

    # Determine readiness: critical requirements must be OK
    # Database is strictly required
    ready = db_ok
    if not ci_health.get("ready", True):
        ready = False
    if not cd_health.get("ready", True):
        ready = False
    if not cosign_health.get("ready", True):
        ready = False
    if not secrets_health.get("authTokensFile", {}).get("safe", True):
        ready = False

    status_str = "ok" if ready else "unavailable"
    if ready and (not db_ok or any(not v.get("ready", True) for v in [ci_health, cd_health, dcim_health, cosign_health])):
        status_str = "degraded"

    details = {
        "status": status_str,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "database": {
            "ready": db_ok,
            "delivery": delivery_health,
            "portal": portal_health.get("status", "unknown"),
        },
        "ci": ci_health,
        "cd": cd_health,
        "dcim": dcim_health,
        "cosign": cosign_health,
        "secrets": secrets_health,
        # Not a readiness condition: an installation without progressive delivery is
        # complete. Reported so "why does canary answer 501?" is answered here.
        "traffic": traffic_health,
    }
    return ready, details


def check_traffic_router() -> dict[str, Any]:
    from .traffic import default_traffic_router

    describe = getattr(default_traffic_router, "describe", None)
    if describe is None:
        return {"mode": getattr(default_traffic_router, "mode", "memory"), "status": "local_only", "ready": True}
    try:
        return describe()
    except Exception as exc:  # a kubectl that cannot run is a fact to report, not to raise from /readyz
        return {"mode": getattr(default_traffic_router, "mode", "unknown"), "status": "unavailable",
                "ready": False, "error": type(exc).__name__, "errorDetail": str(exc)[-300:]}


#: Keys carrying an exception's own text. `/readyz` is unauthenticated -- it is exempt
#: from the rate limiter too, because a load balancer polls it -- and a probe failure's
#: text names the thing that failed: the Jenkins URL, the DCIM endpoint, a secret file
#: path. `/operator/health` exists for exactly that detail and sits behind AdminAccess,
#: so the split is the one the code already draws, not a new one.
_OPERATOR_ONLY_KEYS = ("errorDetail",)


def without_operator_detail(details: dict[str, Any]) -> dict[str, Any]:
    """The readiness report with each probe's raw exception text removed."""

    def strip(value: Any) -> Any:
        if isinstance(value, dict):
            return {k: strip(v) for k, v in value.items() if k not in _OPERATOR_ONLY_KEYS}
        if isinstance(value, list):
            return [strip(item) for item in value]
        return value

    return strip(details)
