"""DCIM integration behind a small, truthful catalog interface."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Protocol


class DcimUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class DcimPage:
    source: str
    status: str
    items: list[dict[str, object]]


class DcimCatalog(Protocol):
    def search_services(self, query: str) -> DcimPage: ...

    def list_modules(self, system_id: str) -> DcimPage: ...

    def list_servers(self, system_id: str, module_id: str | None = None) -> DcimPage: ...


class UnconfiguredDcimCatalog:
    """An explicit empty integration, never a sample-data fallback."""

    def search_services(self, query: str) -> DcimPage:
        return DcimPage("dcim", "not_configured", [])

    def list_modules(self, system_id: str) -> DcimPage:
        return DcimPage("dcim", "not_configured", [])

    def list_servers(self, system_id: str, module_id: str | None = None) -> DcimPage:
        return DcimPage("dcim", "not_configured", [])


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
