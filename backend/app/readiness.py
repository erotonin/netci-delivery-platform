"""Readiness, circuit breakers, and truthful dependency health probing.

Readiness (/readyz) verifies whether the platform can actually accept and execute work,
failing closed (HTTP 503) if mandatory dependencies are unreachable or unconfigured.
Liveness (/livez) answers whether the process is alive.

Detailed operator health (/operator/health) provides authenticated diagnostic details
without leaking secrets or private filesystem paths.
"""

from __future__ import annotations

import os
import shutil
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
                results["authTokensFile"] = {"status": "error", "error": str(exc), "safe": False}
    else:
        results["authTokensFile"] = {"status": "not_configured", "safe": True}

    workload_keys = os.getenv("NETCI_WORKLOAD_TOKEN_KEYS", "").strip()
    results["workloadTokenKeys"] = {
        "status": "configured" if workload_keys else "not_configured",
        "keyCount": len(workload_keys.split(",")) if workload_keys else 0,
    }

    return results


def check_cosign() -> dict[str, Any]:
    """Check Cosign binary and verification key presence if signature verification is active."""
    mode = os.getenv("NETCI_SIGNATURE_VERIFY_MODE", "none").strip().lower()
    if mode != "cosign":
        return {"mode": mode, "status": "not_configured", "optional": True}

    cosign_path = shutil.which("cosign")
    if not cosign_path:
        return {
            "mode": "cosign",
            "status": "unavailable",
            "error": "cosign binary not found in PATH",
            "ready": False,
        }

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

    return {"mode": "cosign", "status": "ready", "ready": True}


def check_ci(launcher: Any) -> dict[str, Any]:
    mode = getattr(launcher, "mode", "none")
    if mode == "jenkins":
        try:
            launcher.refresh_health()
            controllers = getattr(launcher.router, "controllers", [])
            healthy_count = sum(1 for c in controllers if getattr(c, "state", None) and c.state.value == "healthy")
            ready = healthy_count > 0
            return {
                "mode": "jenkins",
                "status": "ready" if ready else "unavailable",
                "healthyControllers": healthy_count,
                "totalControllers": len(controllers),
                "ready": ready,
            }
        except Exception as exc:
            return {"mode": "jenkins", "status": "unavailable", "error": str(exc), "ready": False}
    return {"mode": mode, "status": "not_configured", "optional": True, "ready": True}


def check_cd(orchestrator: Any) -> dict[str, Any]:
    mode = getattr(orchestrator, "mode", "none")
    if mode == "temporal":
        return {
            "mode": "temporal",
            "status": "configured",
            "address": getattr(orchestrator, "address", "unknown"),
            "namespace": getattr(orchestrator, "namespace", "default"),
            "ready": True,
        }
    return {"mode": mode, "status": "not_configured", "optional": True, "ready": True}


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
                return {"status": "unavailable", "error": str(exc), "ready": False}
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
    }
    return ready, details
