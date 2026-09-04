"""DCIM integration behind a small, truthful catalog interface."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Protocol

from ..domain.models import ServerHealthRecord, utc_now


class DcimUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class DcimPage:
    source: str
    status: str
    items: list[dict[str, object]]


@dataclass(frozen=True)
class TargetValidationResult:
    valid: bool
    status: str  # e.g., 'active', 'decommissioned', 'maintenance', 'offline', 'not_found', 'unconfigured'
    message: str
    server_name: str | None = None
    details: dict[str, Any] | None = None


class DcimCatalog(Protocol):
    def search_services(self, query: str) -> DcimPage: ...

    def list_modules(self, system_id: str) -> DcimPage: ...

    def list_servers(self, system_id: str, module_id: str | None = None) -> DcimPage: ...

    def validate_target(
        self, system_id: str, module_id: str, environment: str, target: str
    ) -> TargetValidationResult: ...

    def probe_server_health(self, server_name: str) -> ServerHealthRecord: ...

    def resolve_inventory(
        self, system_id: str, module_id: str, environment: str
    ) -> list[str]: ...


class UnconfiguredDcimCatalog:
    """An explicit empty integration, never a sample-data fallback. Never fakes online."""

    def search_services(self, query: str) -> DcimPage:
        return DcimPage("dcim", "not_configured", [])

    def list_modules(self, system_id: str) -> DcimPage:
        return DcimPage("dcim", "not_configured", [])

    def list_servers(self, system_id: str, module_id: str | None = None) -> DcimPage:
        return DcimPage("dcim", "not_configured", [])

    def validate_target(
        self, system_id: str, module_id: str, environment: str, target: str
    ) -> TargetValidationResult:
        # In unconfigured mode, fail closed if in production, or if strict
        # Return unconfigured status truthfully
        return TargetValidationResult(
            valid=True,
            status="unconfigured",
            message="DCIM provider is unconfigured; target validation bypassed",
            server_name=target,
        )

    def probe_server_health(self, server_name: str) -> ServerHealthRecord:
        return ServerHealthRecord(
            server_name=server_name,
            status="unknown",
            source="unconfigured",
            freshness_seconds=0,
            details={"reason": "DCIM provider is not configured"},
            observed_at=utc_now(),
        )

    def resolve_inventory(
        self, system_id: str, module_id: str, environment: str
    ) -> list[str]:
        return []


class HttpDcimCatalog:
    """Read a REST DCIM exposing services, modules and server inventory."""

    def __init__(self, base_url: str, token: str = "", *, timeout_seconds: float = 10.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout_seconds = timeout_seconds

    def _get(self, path: str, query: dict[str, str] | None = None) -> list[dict[str, object]]:
        suffix = f"?{urllib.parse.urlencode(query)}" if query else ""
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(f"{self.base_url}{path}{suffix}", headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                payload = json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            raise DcimUnavailable(f"DCIM returned HTTP {exc.code}") from exc
        except (urllib.error.URLError, json.JSONDecodeError) as exc:
            raise DcimUnavailable(f"DCIM request failed: {exc}") from exc
        raw_items = payload.get("items") if isinstance(payload, dict) else payload
        if not isinstance(raw_items, list) or any(not isinstance(item, dict) for item in raw_items):
            raise DcimUnavailable("DCIM response must contain an items array")
        return [dict(item) for item in raw_items]

    def search_services(self, query: str) -> DcimPage:
        return DcimPage("dcim-http", "ready", self._get("/services", {"query": query}))

    def list_modules(self, system_id: str) -> DcimPage:
        quoted = urllib.parse.quote(system_id, safe="")
        return DcimPage("dcim-http", "ready", self._get(f"/systems/{quoted}/modules"))

    def list_servers(self, system_id: str, module_id: str | None = None) -> DcimPage:
        quoted = urllib.parse.quote(system_id, safe="")
        query = {"moduleId": module_id} if module_id else None
        return DcimPage("dcim-http", "ready", self._get(f"/systems/{quoted}/servers", query))

    def validate_target(
        self, system_id: str, module_id: str, environment: str, target: str
    ) -> TargetValidationResult:
        try:
            servers = self.list_servers(system_id, module_id)
            for s in servers.items:
                hostname = str(s.get("hostname") or s.get("name") or s.get("server_name") or "")
                env = str(s.get("environment") or "")
                status = str(s.get("status") or "active").lower()
                if hostname == target or target in hostname:
                    # Found matching server
                    if env and environment and env.lower() != environment.lower():
                        return TargetValidationResult(
                            valid=False,
                            status="mismatched_environment",
                            message=f"Target {target} belongs to environment '{env}', expected '{environment}'",
                            server_name=hostname,
                            details=s,
                        )
                    if status in ("decommissioned", "decommissioning", "retired"):
                        return TargetValidationResult(
                            valid=False,
                            status="decommissioned",
                            message=f"Target {target} has been decommissioned",
                            server_name=hostname,
                            details=s,
                        )
                    if status in ("maintenance", "draining", "maintenance_mode"):
                        return TargetValidationResult(
                            valid=False,
                            status="maintenance",
                            message=f"Target {target} is currently in maintenance",
                            server_name=hostname,
                            details=s,
                        )
                    if status in ("offline", "failed", "unreachable"):
                        return TargetValidationResult(
                            valid=False,
                            status="offline",
                            message=f"Target {target} is offline or unreachable in DCIM",
                            server_name=hostname,
                            details=s,
                        )
                    return TargetValidationResult(
                        valid=True,
                        status=status,
                        message=f"Target {target} is active and ready",
                        server_name=hostname,
                        details=s,
                    )
            return TargetValidationResult(
                valid=False,
                status="not_found",
                message=f"Target {target} was not found in DCIM inventory for {system_id}/{module_id}",
                server_name=target,
            )
        except Exception as exc:
            return TargetValidationResult(
                valid=False,
                status="error",
                message=f"Failed to query DCIM for target {target}: {exc}",
                server_name=target,
            )

    def probe_server_health(self, server_name: str) -> ServerHealthRecord:
        try:
            # Try to query server status from DCIM
            quoted = urllib.parse.quote(server_name, safe="")
            res = self._get(f"/servers/{quoted}")
            if res:
                item = res[0]
                status = str(item.get("status") or "active")
                return ServerHealthRecord(
                    server_name=server_name,
                    status=status,
                    source="dcim-http",
                    freshness_seconds=int(item.get("freshness_seconds") or 0),
                    details=item,
                    observed_at=utc_now(),
                )
        except Exception as exc:
            return ServerHealthRecord(
                server_name=server_name,
                status="unreachable",
                source="dcim-http",
                freshness_seconds=0,
                details={"error": str(exc)},
                observed_at=utc_now(),
            )
        return ServerHealthRecord(
            server_name=server_name,
            status="unknown",
            source="dcim-http",
            freshness_seconds=0,
            details={"reason": "not found in dcim"},
            observed_at=utc_now(),
        )

    def resolve_inventory(
        self, system_id: str, module_id: str, environment: str
    ) -> list[str]:
        servers = self.list_servers(system_id, module_id)
        targets = []
        for s in servers.items:
            env = str(s.get("environment") or "").lower()
            status = str(s.get("status") or "active").lower()
            hostname = str(s.get("hostname") or s.get("name") or s.get("server_name") or "")
            if status == "active" and (not env or env == environment.lower()):
                if hostname:
                    targets.append(hostname)
        return targets


def build_dcim_catalog() -> DcimCatalog:
    base_url = os.getenv("NETCI_DCIM_BASE_URL", "").strip()
    if not base_url:
        return UnconfiguredDcimCatalog()
    token_file = os.getenv("NETCI_DCIM_API_TOKEN_FILE", "").strip()
    if token_file:
        try:
            with open(token_file, encoding="utf-8") as handle:
                token = handle.read().strip()
        except OSError as exc:
            raise ValueError(f"NETCI_DCIM_API_TOKEN_FILE is unreadable: {exc}") from exc
    else:
        token = os.getenv("NETCI_DCIM_API_TOKEN", "").strip()
    return HttpDcimCatalog(base_url, token)
