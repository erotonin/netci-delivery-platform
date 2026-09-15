"""DCIM integration behind a small, truthful catalog interface."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from ..domain.models import ServerHealthRecord, ServerMaintenanceState, utc_now


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
    status: str  # e.g., 'active', 'decommissioned', 'maintenance', 'offline', 'not_found', 'unconfigured', 'resource_exhausted'
    message: str
    server_name: str | None = None
    details: dict[str, Any] | None = None


@dataclass(frozen=True)
class ServerTelemetry:
    cpu_percent: float = 0.0
    mem_percent: float = 0.0
    disk_percent: float = 0.0
    observed_at: datetime = field(default_factory=utc_now)


_MAINTENANCE_REGISTRY: dict[str, ServerMaintenanceState] = {}
_TELEMETRY_REGISTRY: dict[str, Any] = {}


def set_server_maintenance(server_name: str, in_maintenance: bool, reason: str = "", operator: str = "operator") -> ServerMaintenanceState:
    state = ServerMaintenanceState(
        server_name=server_name,
        in_maintenance=in_maintenance,
        reason=reason,
        updated_by=operator,
        updated_at=utc_now(),
    )
    _MAINTENANCE_REGISTRY[server_name] = state
    return state


def get_server_maintenance(server_name: str) -> ServerMaintenanceState | None:
    return _MAINTENANCE_REGISTRY.get(server_name)


def list_server_maintenance_states() -> list[ServerMaintenanceState]:
    return list(_MAINTENANCE_REGISTRY.values())


def update_server_telemetry(server_name: str, cpu_percent: float, memory_percent: float, disk_percent: float) -> dict[str, Any]:
    record = {
        "server_name": server_name,
        "cpu_percent": cpu_percent,
        "memory_percent": memory_percent,
        "disk_percent": disk_percent,
        "updated_at": utc_now().isoformat(),
    }
    _TELEMETRY_REGISTRY[server_name] = record
    return record


def get_server_telemetry(server_name: str) -> dict[str, Any] | None:
    return _TELEMETRY_REGISTRY.get(server_name)


def check_preflight_telemetry(server_name: str) -> TargetValidationResult | None:
    m_state = get_server_maintenance(server_name)
    if m_state and m_state.in_maintenance:
        return TargetValidationResult(
            valid=False,
            status="maintenance",
            message=f"Target {server_name} is currently in maintenance mode: {m_state.reason or 'Scheduled maintenance'}",
            server_name=server_name,
            details={"in_maintenance": True, "reason": m_state.reason},
        )

    telemetry = get_server_telemetry(server_name)
    if telemetry:
        disk = float(telemetry.get("disk_percent") or 0)
        cpu = float(telemetry.get("cpu_percent") or 0)
        if disk > 90.0:
            return TargetValidationResult(
                valid=False,
                status="resource_exhausted",
                message=f"Target {server_name} disk usage ({disk:.1f}%) exceeds safety threshold (90%)",
                server_name=server_name,
                details=telemetry,
            )
        if cpu > 95.0:
            return TargetValidationResult(
                valid=False,
                status="resource_exhausted",
                message=f"Target {server_name} CPU usage ({cpu:.1f}%) exceeds safety threshold (95%)",
                server_name=server_name,
                details=telemetry,
            )
    return TargetValidationResult(
        valid=True,
        status="healthy",
        message=f"Target {server_name} passed pre-flight telemetry checks",
        server_name=server_name,
        details=telemetry,
    )


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
        preflight = check_preflight_telemetry(target)
        if preflight:
            return preflight

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
                    maint_rec = _MAINTENANCE_REGISTRY.get(hostname)
                    if maint_rec and maint_rec.in_maintenance:
                        return TargetValidationResult(
                            valid=False,
                            status="maintenance",
                            message=f"Target {target} is in NetCI maintenance mode: {maint_rec.reason}",
                            server_name=hostname,
                            details={"maintenance": maint_rec.__dict__},
                        )
                    telem_gate = check_preflight_telemetry(hostname)
                    if not telem_gate.valid:
                        return telem_gate
                    return TargetValidationResult(
                        valid=True,
                        status=status,
                        message=f"Target {target} is active and passed pre-flight telemetry gate",
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
    provider = os.getenv("NETCI_DCIM_PROVIDER", "http").strip().lower()
    if provider == "netbox":
        from .netbox_dcim import NetBoxDcimCatalog

        return NetBoxDcimCatalog(base_url, token)
    if provider != "http":
        raise ValueError(f"unsupported NETCI_DCIM_PROVIDER: {provider!r} (http, netbox)")
    return HttpDcimCatalog(base_url, token)
