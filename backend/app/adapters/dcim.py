"""DCIM integration behind a small, truthful catalog interface."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from typing import Any, Protocol

from ..domain.models import ServerHealthRecord, ServerMaintenanceState, ServerTelemetry, utc_now


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


class ServerStateStore(Protocol):
    """Where maintenance mode and the latest telemetry live: the platform database.

    Both decide whether a deployment may proceed, so both are durable state (CLAUDE.md
    non-negotiable #3). They used to be process-local dicts: an API restart forgot every
    server in maintenance, and a second replica never saw what the first was told.
    """

    def get_maintenance(self, server_name: str) -> ServerMaintenanceState | None: ...

    def set_maintenance(self, state: ServerMaintenanceState) -> None: ...

    def list_maintenance(self) -> list[ServerMaintenanceState]: ...

    def get_telemetry(self, server_name: str) -> ServerTelemetry | None: ...

    def record_telemetry(self, telemetry: ServerTelemetry) -> None: ...


class InMemoryServerState:
    """For tests and the in-memory database. Not a cache: it *is* the store there."""

    def __init__(self) -> None:
        self.maintenance: dict[str, ServerMaintenanceState] = {}
        self.telemetry: dict[str, ServerTelemetry] = {}

    def get_maintenance(self, server_name: str) -> ServerMaintenanceState | None:
        return self.maintenance.get(server_name)

    def set_maintenance(self, state: ServerMaintenanceState) -> None:
        self.maintenance[state.server_name] = state

    def list_maintenance(self) -> list[ServerMaintenanceState]:
        return list(self.maintenance.values())

    def get_telemetry(self, server_name: str) -> ServerTelemetry | None:
        return self.telemetry.get(server_name)

    def record_telemetry(self, telemetry: ServerTelemetry) -> None:
        self.telemetry[telemetry.server_name] = telemetry


class DatabaseServerState:
    """Reads and writes through the platform database, one short transaction each."""

    def __init__(self, database: Any) -> None:
        self.database = database

    def get_maintenance(self, server_name: str) -> ServerMaintenanceState | None:
        with self.database.transaction() as session:
            return session.get_server_maintenance(server_name)

    def set_maintenance(self, state: ServerMaintenanceState) -> None:
        with self.database.transaction() as session:
            session.upsert_server_maintenance(state)

    def list_maintenance(self) -> list[ServerMaintenanceState]:
        with self.database.transaction() as session:
            return list(session.list_server_maintenance())

    def get_telemetry(self, server_name: str) -> ServerTelemetry | None:
        with self.database.transaction() as session:
            return session.get_server_telemetry(server_name)

    def record_telemetry(self, telemetry: ServerTelemetry) -> None:
        with self.database.transaction() as session:
            session.upsert_server_telemetry(telemetry)


_server_state: ServerStateStore = InMemoryServerState()


def configure_server_state(store: ServerStateStore) -> None:
    """Bind the module to the platform database. main.py does this at import time."""

    global _server_state
    _server_state = store


def server_state() -> ServerStateStore:
    return _server_state


def set_server_maintenance(server_name: str, in_maintenance: bool, reason: str = "", operator: str = "operator") -> ServerMaintenanceState:
    state = ServerMaintenanceState(
        server_name=server_name,
        in_maintenance=in_maintenance,
        reason=reason,
        updated_by=operator,
        updated_at=utc_now(),
    )
    _server_state.set_maintenance(state)
    return state


def get_server_maintenance(server_name: str) -> ServerMaintenanceState | None:
    return _server_state.get_maintenance(server_name)


def list_server_maintenance_states() -> list[ServerMaintenanceState]:
    return _server_state.list_maintenance()


def update_server_telemetry(server_name: str, cpu_percent: float, memory_percent: float, disk_percent: float) -> ServerTelemetry:
    record = ServerTelemetry(
        server_name=server_name,
        cpu_percent=float(cpu_percent),
        mem_percent=float(memory_percent),
        disk_percent=float(disk_percent),
        observed_at=utc_now(),
    )
    _server_state.record_telemetry(record)
    return record


def get_server_telemetry(server_name: str) -> ServerTelemetry | None:
    return _server_state.get_telemetry(server_name)


def check_preflight_telemetry(server_name: str) -> TargetValidationResult:
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
    details = (
        {
            "cpu_percent": telemetry.cpu_percent,
            "mem_percent": telemetry.mem_percent,
            "disk_percent": telemetry.disk_percent,
            "observed_at": telemetry.observed_at.isoformat(),
        }
        if telemetry
        else None
    )
    if telemetry:
        if telemetry.disk_percent > 90.0:
            return TargetValidationResult(
                valid=False,
                status="resource_exhausted",
                message=f"Target {server_name} disk usage ({telemetry.disk_percent:.1f}%) exceeds safety threshold (90%)",
                server_name=server_name,
                details=details,
            )
        if telemetry.cpu_percent > 95.0:
            return TargetValidationResult(
                valid=False,
                status="resource_exhausted",
                message=f"Target {server_name} CPU usage ({telemetry.cpu_percent:.1f}%) exceeds safety threshold (95%)",
                server_name=server_name,
                details=details,
            )
    return TargetValidationResult(
        valid=True,
        status="healthy",
        message=f"Target {server_name} passed pre-flight telemetry checks",
        server_name=server_name,
        details=details,
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
        # Only a refusal is worth returning. The helper's pass result says `healthy`,
        # and an unconfigured inventory has no basis to say that about any host.
        if not preflight.valid:
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
                    maint_rec = get_server_maintenance(hostname)
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
