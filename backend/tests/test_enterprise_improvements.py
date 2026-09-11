"""Enterprise improvements test suite:
1. Security Waiver (VEX) API & Policy Engine integration.
2. Server Maintenance & Telemetry Pre-flight Gate.
3. Outbound Runner Agent WebSocket Protocol (Zero Inbound Port).
4. L7 Header/Cookie Canary Routing.
5. PostgreSQL Connection Pool robust configuration.
"""

import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.adapters.dcim import (
    HttpDcimCatalog,
    UnconfiguredDcimCatalog,
    _MAINTENANCE_REGISTRY,
    _TELEMETRY_REGISTRY,
    ServerTelemetry,
    check_preflight_telemetry,
    set_server_maintenance,
    update_server_telemetry,
)
from app.domain.models import (
    SecurityWaiver,
    ServerMaintenanceState,
    WaiverStatus,
    utc_now,
)
from app.main import app
from app.policy.engine import BuiltinPolicyEngine
from app.store.memory import InMemoryDatabase
from app.traffic import InMemoryTrafficRoutingAdapter


def test_security_waivers_api():
    client = TestClient(app)
    future = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()

    # 1. Create a security waiver
    res = client.post(
        "/api/v1/security/waivers",
        json={
            "cveId": "CVE-2026-9999",
            "moduleId": "billing-api",
            "reason": "VEX: Component isolated from untrusted input, patch scheduled",
            "expiresAt": future,
        },
    )
    assert res.status_code == 201, res.text
    waiver = res.json()
    waiver_id = waiver["id"]
    assert waiver["cveId"] == "CVE-2026-9999"
    assert waiver["status"] == "active"
    assert waiver["isValid"] is True

    # 2. List security waivers
    list_res = client.get("/api/v1/security/waivers", params={"activeOnly": True})
    assert list_res.status_code == 200
    items = list_res.json()
    assert any(w["id"] == waiver_id for w in items)

    # 3. Revoke security waiver
    revoke_res = client.post(f"/api/v1/security/waivers/{waiver_id}/revoke")
    assert revoke_res.status_code == 200
    assert revoke_res.json()["status"] == "revoked"

    # 4. Expired date rejection
    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    bad_res = client.post(
        "/api/v1/security/waivers",
        json={
            "cveId": "CVE-2026-8888",
            "reason": "Past date test",
            "expiresAt": past,
        },
    )
    assert bad_res.status_code == 422


def test_security_waiver_policy_engine_bypass():
    mem_db = InMemoryDatabase()
    future = datetime.now(timezone.utc) + timedelta(days=30)

    # Add active waiver for CVE-2026-CRITICAL
    waiver = SecurityWaiver(
        id=uuid4(),
        cve_id="CVE-2026-CRITICAL",
        reason="VEX waiver: false positive under our sandboxed configuration",
        approved_by="sec-lead",
        expires_at=future,
        status=WaiverStatus.ACTIVE,
    )
    with mem_db.transaction() as session:
        session.insert_security_waiver(waiver)

        # Artifact evidence with critical vulnerability
        evidence = {
            "artifactDigest": "sha256:" + "a" * 64,
            "sbom": {"generatedBy": "syft", "location": "oci://registry/sbom.json"},
            "vulnerabilityScan": {
                "scanner": "trivy",
                "status": "failed",
                "critical": 1,
                "high": 0,
                "findings": ["CVE-2026-CRITICAL"],
            },
            "signature": {"provider": "cosign", "verified": True},
        }

        # Admission evaluation should PASS because of waiver
        result = BuiltinPolicyEngine.evaluate_artifact_admission(
            session=session,
            evidence=evidence,
            expected_digest="sha256:" + "a" * 64,
            require_evidence=True,
        )
        assert result.allowed is True, f"Expected waiver to bypass critical CVE, reason: {result.reason}"


def test_server_maintenance_and_telemetry_gate():
    server = "test-prod-srv-01"

    # Initially normal
    update_server_telemetry(server, cpu_percent=20.0, memory_percent=40.0, disk_percent=50.0)
    gate_normal = check_preflight_telemetry(server)
    assert gate_normal.valid is True

    # High disk pre-flight gate failure
    update_server_telemetry(server, cpu_percent=20.0, memory_percent=40.0, disk_percent=95.0)
    gate_disk = check_preflight_telemetry(server)
    assert gate_disk.valid is False
    assert gate_disk.status == "resource_exhausted"
    assert "disk usage" in gate_disk.message.lower()

    # High CPU pre-flight gate failure
    update_server_telemetry(server, cpu_percent=98.0, memory_percent=40.0, disk_percent=50.0)
    gate_cpu = check_preflight_telemetry(server)
    assert gate_cpu.valid is False
    assert gate_cpu.status == "resource_exhausted"
    assert "cpu usage" in gate_cpu.message.lower()

    # Maintenance mode blocks deployment
    set_server_maintenance(server, in_maintenance=True, reason="Hardware upgrade in progress")
    gate_maint = check_preflight_telemetry(server)
    assert gate_maint.valid is False
    assert gate_maint.status == "maintenance"

    # Reset maintenance
    set_server_maintenance(server, in_maintenance=False)
    update_server_telemetry(server, cpu_percent=25.0, memory_percent=35.0, disk_percent=40.0)
    assert check_preflight_telemetry(server).valid is True


def test_server_maintenance_api():
    client = TestClient(app)
    server_name = "api-prod-edge-02"

    # Set maintenance via API
    res = client.post(
        f"/api/v1/servers/{server_name}/maintenance",
        json={"inMaintenance": True, "reason": "Kernel security patching", "updatedBy": "ops-admin"},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["serverName"] == server_name
    assert data["inMaintenance"] is True
    assert data["reason"] == "Kernel security patching"

    # Check telemetry API
    telem_res = client.get(f"/api/v1/servers/{server_name}/telemetry")
    assert telem_res.status_code == 200
    telem_data = telem_res.json()
    assert "cpuPercent" in telem_data
    assert "diskPercent" in telem_data

    # Turn off maintenance
    res2 = client.post(
        f"/api/v1/servers/{server_name}/maintenance",
        json={"inMaintenance": False, "reason": "Patching complete", "updatedBy": "ops-admin"},
    )
    assert res2.status_code == 200
    assert res2.json()["inMaintenance"] is False


def test_l7_canary_routing():
    router = InMemoryTrafficRoutingAdapter()
    app_id = "app-order-service"
    env = "prod"

    # Set Canary traffic weight to 10%
    router.set_traffic_weight(app_id, env, canary_weight=10)

    # Configure L7 rules: header X-Beta-Tester=true or cookie beta_user=1
    router.set_canary_rules(
        app_id,
        env,
        {
            "header_name": "X-Beta-Tester",
            "header_value": "true",
            "cookie": "beta_user=1",
        },
    )

    # Request with matching header routes to canary
    route1 = router.match_route(app_id, env, headers={"X-Beta-Tester": "true"})
    assert route1 == "canary"

    # Request with matching cookie routes to canary
    route2 = router.match_route(app_id, env, cookies={"beta_user": "1"})
    assert route2 == "canary"

    # Regular user without header/cookie routes to baseline
    route3 = router.match_route(app_id, env, headers={"User-Agent": "Mozilla"}, cookies={})
    assert route3 == "baseline"


@pytest.mark.asyncio
async def test_runner_agent_websocket():
    from fastapi import WebSocketDisconnect
    from app.main import runner_agent_websocket

    class MockWebSocket:
        def __init__(self):
            self.accepted = False
            self.sent = []
            self.messages = [
                json.dumps(
                    {
                        "type": "TELEMETRY_HEARTBEAT",
                        "agent_id": "runner-test-01",
                        "hostname": "test-srv",
                        "telemetry": {
                            "cpu_percent": 18.5,
                            "mem_percent": 42.0,
                            "disk_percent": 33.1,
                        },
                    }
                )
            ]

        async def accept(self):
            self.accepted = True

        async def receive_text(self):
            if self.messages:
                return self.messages.pop(0)
            raise WebSocketDisconnect(1000)

        async def send_text(self, text):
            self.sent.append(text)

    mock_ws = MockWebSocket()
    await runner_agent_websocket(mock_ws, agent_id="runner-test-01", hostname="test-srv")
    assert mock_ws.accepted is True
    assert len(mock_ws.sent) == 1
    assert "HEARTBEAT_ACK" in mock_ws.sent[0]

    # Verify telemetry was updated in memory registry
    assert "test-srv" in _TELEMETRY_REGISTRY
    record = _TELEMETRY_REGISTRY["test-srv"]
    assert record.cpu_percent == 18.5
    assert record.disk_percent == 33.1


@pytest.mark.asyncio
async def test_outbound_runner_daemon_execution():
    from app.adapters.agent_daemon import collect_host_telemetry, NetCiAgentDaemon

    telem = collect_host_telemetry()
    assert "cpu_percent" in telem
    assert "disk_percent" in telem
    assert "mem_percent" in telem

    daemon = NetCiAgentDaemon("ws://localhost:8100", "test-agent", "test-host")
    res = await daemon._run_command("echo 'NetCI Zero Inbound Port'")
    assert res["exit_code"] == 0
    assert "NetCI Zero Inbound Port" in res["output"]
