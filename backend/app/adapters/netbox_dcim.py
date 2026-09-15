"""DCIM catalog backed by NetBox -- the inventory system, not a copy of it.

netCI asks its DCIM three questions: which systems exist, which modules a system has,
and which servers a module runs on in a given environment -- plus, right before a
deployment, "is this target still one I may deploy to?". NetBox answers all of them from
its own model, which is mapped as follows and nowhere else:

    netCI system      <- NetBox tenant          (the unit that owns the service)
    netCI module      <- NetBox device role     (what the device does for the tenant)
    netCI server      <- NetBox device          (the machine itself)
    netCI environment <- NetBox site            (dev / staging / prod)
    netCI status      <- NetBox device status   (active / offline / failed / decommissioning ...)

Two things NetBox can and cannot tell netCI, kept separate on purpose:

* NetBox **can** say a device is decommissioning, failed or offline. That is inventory
  truth and it blocks a deployment.
* NetBox **cannot** say a device is healthy *right now*. `active` means "in service
  according to the inventory", not "responded a moment ago". `probe_server_health`
  therefore reports `unknown` for an active device, with the inventory status in the
  details -- never `online`. A green light this adapter did not earn is exactly what
  the platform exists to refuse.

Configuration: `NETCI_DCIM_PROVIDER=netbox`, `NETCI_DCIM_BASE_URL=https://netbox/api`,
and a NetBox API token via `NETCI_DCIM_API_TOKEN_FILE` (preferred) or
`NETCI_DCIM_API_TOKEN`.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from ..domain.models import ServerHealthRecord, utc_now
from .dcim import (
    DcimPage,
    DcimUnavailable,
    TargetValidationResult,
    _MAINTENANCE_REGISTRY,
    check_preflight_telemetry,
)

logger = logging.getLogger(__name__)

#: NetBox device statuses that mean "not a place to deploy to". Everything else is
#: reported as-is; the caller decides what `planned` or `staged` means for it.
BLOCKING_STATUSES = frozenset({"decommissioning", "failed", "offline"})


class NetBoxDcimCatalog:
    source = "netbox"

    def __init__(self, base_url: str, token: str, *, timeout_seconds: float = 10.0) -> None:
        if not token:
            raise ValueError("NetBox requires an API token (NETCI_DCIM_API_TOKEN_FILE)")
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout_seconds = timeout_seconds

    # ------------------------------------------------------------------ transport

    def _get(self, path: str, query: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """Follow NetBox pagination; a partial inventory is a wrong inventory."""

        params = dict(query or {})
        params.setdefault("limit", 200)
        url = f"{self.base_url}{path}?{urllib.parse.urlencode(params, doseq=True)}"
        results: list[dict[str, Any]] = []
        while url:
            request = urllib.request.Request(
                url, headers={"Authorization": f"Token {self.token}", "Accept": "application/json"}
            )
            try:
                with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                    payload = json.loads(response.read() or b"{}")
            except urllib.error.HTTPError as exc:
                # 401/403 mean the token is wrong; say so without echoing it.
                raise DcimUnavailable(f"NetBox returned HTTP {exc.code} for {path}") from exc
            except (urllib.error.URLError, json.JSONDecodeError) as exc:
                raise DcimUnavailable(f"NetBox request failed: {exc}") from exc
            if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
                raise DcimUnavailable("NetBox response did not contain a results array")
            results.extend(item for item in payload["results"] if isinstance(item, dict))
            url = payload.get("next") or ""
        return results

    # ------------------------------------------------------------------- mapping

    @staticmethod
    def _slug(value: Any) -> str:
        if isinstance(value, dict):
            return str(value.get("slug") or value.get("name") or "")
        return str(value or "")

    @staticmethod
    def _system(tenant: dict[str, Any]) -> dict[str, object]:
        return {
            "id": str(tenant.get("slug") or ""),
            "name": str(tenant.get("name") or ""),
            "unit": NetBoxDcimCatalog._slug(tenant.get("group")) or "unassigned",
            "description": str(tenant.get("description") or ""),
            "source": "netbox",
            "netboxId": tenant.get("id"),
        }

    def _device(self, device: dict[str, Any]) -> dict[str, object]:
        status = device.get("status") or {}
        primary_ip = device.get("primary_ip4") or device.get("primary_ip") or {}
        return {
            "id": str(device.get("name") or ""),
            "hostname": str(device.get("name") or ""),
            "name": str(device.get("name") or ""),
            "ipAddress": str(primary_ip.get("address") or "").split("/")[0] if isinstance(primary_ip, dict) else "",
            "environment": self._slug(device.get("site")),
            "moduleId": self._slug(device.get("role")),
            "systemId": self._slug(device.get("tenant")),
            "status": str(status.get("value") if isinstance(status, dict) else status or "").lower(),
            "platform": self._slug(device.get("platform")),
            "netboxId": device.get("id"),
            "netboxUrl": device.get("display_url") or device.get("url"),
        }

    # ------------------------------------------------------------------ catalog

    def search_services(self, query: str) -> DcimPage:
        params = {"q": query} if query else {}
        tenants = self._get("/tenancy/tenants/", params)
        return DcimPage(self.source, "ready", [self._system(item) for item in tenants])

    def list_modules(self, system_id: str) -> DcimPage:
        devices = self._get("/dcim/devices/", {"tenant": system_id})
        seen: dict[str, dict[str, object]] = {}
        for device in devices:
            role = device.get("role") or {}
            slug = self._slug(role)
            if not slug or slug in seen:
                continue
            seen[slug] = {
                "id": slug,
                "name": str(role.get("name") or slug) if isinstance(role, dict) else slug,
                "systemId": system_id,
                "description": str(role.get("description") or "") if isinstance(role, dict) else "",
                "source": "netbox",
            }
        return DcimPage(self.source, "ready", list(seen.values()))

    def list_servers(self, system_id: str, module_id: str | None = None) -> DcimPage:
        params: dict[str, Any] = {"tenant": system_id}
        if module_id:
            params["role"] = module_id
        devices = self._get("/dcim/devices/", params)
        return DcimPage(self.source, "ready", [self._device(item) for item in devices])

    def resolve_inventory(self, system_id: str, module_id: str, environment: str) -> list[str]:
        devices = self._get(
            "/dcim/devices/", {"tenant": system_id, "role": module_id, "site": environment}
        )
        return [
            str(item.get("name"))
            for item in devices
            if self._device(item)["status"] not in BLOCKING_STATUSES
        ]

    # -------------------------------------------------------------- validation

    def validate_target(
        self, system_id: str, module_id: str, environment: str, target: str
    ) -> TargetValidationResult:
        try:
            devices = self._get("/dcim/devices/", {"tenant": system_id, "role": module_id, "name": target})
        except DcimUnavailable as exc:
            return TargetValidationResult(
                valid=False, status="error",
                message=f"NetBox could not be queried for {target}: {exc}", server_name=target,
            )
        if not devices:
            return TargetValidationResult(
                valid=False, status="not_found",
                message=f"Target {target} is not in NetBox under tenant {system_id} / role {module_id}",
                server_name=target,
            )
        device = self._device(devices[0])
        if device["environment"] and environment and str(device["environment"]).lower() != environment.lower():
            return TargetValidationResult(
                valid=False, status="mismatched_environment",
                message=f"Target {target} is at site '{device['environment']}', expected '{environment}'",
                server_name=target, details=device,
            )
        status = str(device["status"])
        if status in BLOCKING_STATUSES:
            return TargetValidationResult(
                valid=False,
                status="decommissioned" if status == "decommissioning" else status,
                message=f"Target {target} is {status} in NetBox",
                server_name=target, details=device,
            )
        maintenance = _MAINTENANCE_REGISTRY.get(target)
        if maintenance and maintenance.in_maintenance:
            return TargetValidationResult(
                valid=False, status="maintenance",
                message=f"Target {target} is in netCI maintenance mode: {maintenance.reason}",
                server_name=target, details={"maintenance": maintenance.__dict__},
            )
        telemetry_gate = check_preflight_telemetry(target)
        if not telemetry_gate.valid:
            return telemetry_gate
        return TargetValidationResult(
            valid=True, status=status,
            message=f"Target {target} is {status} in NetBox and passed the pre-flight gate",
            server_name=target, details=device,
        )

    def probe_server_health(self, server_name: str) -> ServerHealthRecord:
        """Inventory status is not live health. An active device is `unknown` here."""

        try:
            devices = self._get("/dcim/devices/", {"name": server_name})
        except DcimUnavailable as exc:
            return ServerHealthRecord(
                server_name=server_name, status="unknown", source="netbox",
                details={"error": str(exc)}, observed_at=utc_now(),
            )
        if not devices:
            return ServerHealthRecord(
                server_name=server_name, status="unknown", source="netbox",
                details={"reason": "not in NetBox"}, observed_at=utc_now(),
            )
        device = self._device(devices[0])
        inventory = str(device["status"])
        if inventory in {"offline", "failed"}:
            live = "offline"
        elif inventory == "decommissioning":
            live = "decommissioned"
        else:
            # NetBox knows the machine is meant to be in service; it does not know that
            # it is. That fact belongs to a telemetry provider, not an inventory.
            live = "unknown"
        return ServerHealthRecord(
            server_name=server_name, status=live, source="netbox",
            details={"inventoryStatus": inventory, "site": device["environment"], "netboxId": device["netboxId"]},
            observed_at=utc_now(),
        )
