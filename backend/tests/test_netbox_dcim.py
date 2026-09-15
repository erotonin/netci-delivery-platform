"""NetBoxDcimCatalog against a real HTTP server speaking NetBox's REST shape.

Tests a real transport (urllib against a local socket), not a patched `_get`: pagination,
the token header, the fail-closed paths and the inventory→netCI mapping are the things
that go wrong against a real NetBox, and a mocked transport cannot catch any of them.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest

from app.adapters.dcim import DcimUnavailable
from app.adapters.netbox_dcim import NetBoxDcimCatalog

TOKEN = "netbox-test-token-do-not-log"


def device(name, *, tenant="payments", role="api", site="dev", status="active", ip="10.0.0.5/24"):
    return {
        "id": abs(hash(name)) % 10_000,
        "name": name,
        "tenant": {"slug": tenant, "name": tenant.title()},
        "role": {"slug": role, "name": role},
        "site": {"slug": site, "name": site},
        "status": {"value": status, "label": status.title()},
        "primary_ip4": {"address": ip},
        "platform": {"slug": "ubuntu"},
        "url": f"http://netbox/api/dcim/devices/{name}/",
    }


class FakeNetBox:
    """Enough of NetBox's REST API to exercise the adapter: filters + pagination."""

    def __init__(self):
        self.devices: list[dict] = []
        self.tenants: list[dict] = []
        self.page_size = 2
        self.requests: list[tuple[str, dict, str]] = []
        self.fail_with: int | None = None

    def filter_devices(self, query: dict) -> list[dict]:
        out = self.devices
        for key, field in (("tenant", "tenant"), ("role", "role"), ("site", "site")):
            if key in query:
                out = [d for d in out if d[field]["slug"] == query[key][0]]
        if "name" in query:
            out = [d for d in out if d["name"] == query["name"][0]]
        return out


@pytest.fixture
def netbox():
    state = FakeNetBox()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):  # keep pytest output quiet
            pass

        def do_GET(self):
            parts = urlsplit(self.path)
            query = parse_qs(parts.query)
            state.requests.append((parts.path, query, self.headers.get("Authorization", "")))
            if self.headers.get("Authorization") != f"Token {TOKEN}":
                self.send_response(403); self.end_headers(); return
            if state.fail_with:
                self.send_response(state.fail_with); self.end_headers(); return
            if parts.path == "/api/dcim/devices/":
                rows = state.filter_devices(query)
            elif parts.path == "/api/tenancy/tenants/":
                q = (query.get("q") or [""])[0].lower()
                rows = [t for t in state.tenants if q in t["name"].lower()]
            else:
                self.send_response(404); self.end_headers(); return
            offset = int((query.get("offset") or ["0"])[0])
            page = rows[offset: offset + state.page_size]
            nxt = None
            if offset + state.page_size < len(rows):
                keep = {k: v[0] for k, v in query.items() if k != "offset"}
                keep["offset"] = str(offset + state.page_size)
                nxt = f"http://{self.headers['Host']}{parts.path}?" + "&".join(f"{k}={v}" for k, v in keep.items())
            body = json.dumps({"count": len(rows), "next": nxt, "results": page}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state.base_url = f"http://127.0.0.1:{server.server_port}/api"
    try:
        yield state
    finally:
        server.shutdown()


def catalog(netbox, token=TOKEN):
    return NetBoxDcimCatalog(netbox.base_url, token, timeout_seconds=5)


def test_a_missing_token_is_refused_at_construction():
    with pytest.raises(ValueError):
        NetBoxDcimCatalog("http://netbox/api", "")


def test_inventory_follows_pagination_so_a_partial_page_is_never_the_inventory(netbox):
    netbox.devices = [device(f"api-dev-0{i}") for i in range(1, 6)]
    hosts = catalog(netbox).resolve_inventory("payments", "api", "dev")
    assert hosts == [f"api-dev-0{i}" for i in range(1, 6)]
    # 5 devices at page size 2 = 3 requests, all carrying the token header.
    device_requests = [r for r in netbox.requests if r[0] == "/api/dcim/devices/"]
    assert len(device_requests) == 3
    assert all(auth == f"Token {TOKEN}" for _, _, auth in device_requests)


def test_inventory_excludes_devices_netbox_says_are_out_of_service(netbox):
    netbox.devices = [
        device("api-dev-01"),
        device("api-dev-02", status="offline"),
        device("api-dev-03", status="decommissioning"),
        device("api-dev-04", status="failed"),
        device("api-dev-05", status="planned"),
    ]
    assert catalog(netbox).resolve_inventory("payments", "api", "dev") == ["api-dev-01", "api-dev-05"]


def test_a_wrong_token_fails_closed_without_echoing_it(netbox):
    netbox.devices = [device("api-dev-01")]
    with pytest.raises(DcimUnavailable) as exc:
        catalog(netbox, token="wrong-token-value").resolve_inventory("payments", "api", "dev")
    assert "403" in str(exc.value)
    assert "wrong-token-value" not in str(exc.value)


def test_an_unreachable_netbox_is_an_error_not_an_empty_inventory(netbox):
    netbox.fail_with = 503
    with pytest.raises(DcimUnavailable):
        catalog(netbox).resolve_inventory("payments", "api", "dev")


@pytest.mark.parametrize(
    "status, expected",
    [("offline", "offline"), ("failed", "failed"), ("decommissioning", "decommissioned")],
)
def test_validate_target_refuses_devices_netbox_marks_out_of_service(netbox, status, expected):
    netbox.devices = [device("api-dev-01", status=status)]
    result = catalog(netbox).validate_target("payments", "api", "dev", "api-dev-01")
    assert result.valid is False
    assert result.status == expected


def test_validate_target_refuses_a_device_from_another_site(netbox):
    """`dev` must never deploy onto the production box even if the name is right."""
    netbox.devices = [device("api-01", site="prod")]
    result = catalog(netbox).validate_target("payments", "api", "dev", "api-01")
    assert result.valid is False
    assert result.status == "mismatched_environment"


def test_validate_target_refuses_a_device_outside_the_module_scope(netbox):
    netbox.devices = [device("db-dev-01", role="db")]
    result = catalog(netbox).validate_target("payments", "api", "dev", "db-dev-01")
    assert result.valid is False
    assert result.status == "not_found"


def test_validate_target_reports_netbox_outage_as_error_not_as_a_valid_target(netbox):
    netbox.fail_with = 500
    result = catalog(netbox).validate_target("payments", "api", "dev", "api-dev-01")
    assert result.valid is False
    assert result.status == "error"


def test_validate_target_accepts_an_active_device_in_the_right_place(netbox):
    netbox.devices = [device("api-dev-01")]
    result = catalog(netbox).validate_target("payments", "api", "dev", "api-dev-01")
    assert result.valid is True
    assert result.details["ipAddress"] == "10.0.0.5"
    assert result.details["environment"] == "dev"


def test_probe_health_does_not_upgrade_inventory_active_to_live_online(netbox):
    """NetBox records intent, not liveness. Reporting `online` from it would be the
    exact kind of fabricated health this platform refuses to emit."""
    netbox.devices = [
        device("api-dev-01"),
        device("api-dev-02", status="offline"),
        device("api-dev-03", status="decommissioning"),
    ]
    c = catalog(netbox)
    assert c.probe_server_health("api-dev-01").status == "unknown"
    assert c.probe_server_health("api-dev-02").status == "offline"
    assert c.probe_server_health("api-dev-03").status == "decommissioned"
    assert c.probe_server_health("ghost").status == "unknown"
    assert c.probe_server_health("api-dev-01").source == "netbox"


def test_search_services_maps_tenants_to_systems(netbox):
    netbox.tenants = [
        {"id": 1, "slug": "payments", "name": "Payments", "group": {"slug": "fintech"}, "description": "cards"},
        {"id": 2, "slug": "search", "name": "Search", "group": None, "description": ""},
    ]
    page = catalog(netbox).search_services("pay")
    items = page.items if hasattr(page, "items") else page["items"]
    assert [s["id"] for s in items] == ["payments"]
    assert items[0]["unit"] == "fintech"
    assert items[0]["source"] == "netbox"


def test_an_unconfigured_dcim_never_calls_a_target_healthy():
    """Found by the readiness self-check: the telemetry pre-flight's *pass* result was
    returned as the unconfigured catalog's answer, so "no inventory" read as "healthy"."""
    from app.adapters.dcim import UnconfiguredDcimCatalog

    result = UnconfiguredDcimCatalog().validate_target("sys", "mod", "prod", "server-prod-01")
    assert result.status == "unconfigured"
    assert "healthy" not in result.message
