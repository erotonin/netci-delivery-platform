"""Tests for Break-Glass Dual-Control Emergency Override."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.policy.break_glass import BreakGlassError, BreakGlassService
from app.policy.engine import BuiltinPolicyEngine
from app.policy.rules import Role
from app.store.memory import InMemoryDatabase

DIGEST = "sha256:" + "b" * 64


def test_break_glass_requires_dual_control():
    db = InMemoryDatabase()
    with db.transaction() as session:
        req = BreakGlassService.create_request(
            session,
            target_type="artifact",
            target_id=DIGEST,
            requested_by="ops-oncall@corp.example",
            reason="P0 production incident hotfix",
            incident_ticket="INC-9999",
        )
        assert req.status == "pending"

        # Requester cannot self-approve
        with pytest.raises(BreakGlassError, match="dual-control"):
            BreakGlassService.approve_request(
                session,
                request_id=req.id,
                approved_by="ops-oncall@corp.example",
            )

        # Second person can approve
        approved = BreakGlassService.approve_request(
            session,
            request_id=req.id,
            approved_by="security-lead@corp.example",
            ttl_minutes=30,
        )
        assert approved.status == "active"
        assert approved.approved_by == "security-lead@corp.example"
        assert approved.expires_at is not None

        # Verify active lookup
        active = BreakGlassService.active_break_glass(session, target_type="artifact", target_id=DIGEST)
        assert active is not None
        assert active.id == req.id


def test_break_glass_overrides_vulnerability_scan_in_policy_engine():
    db = InMemoryDatabase()
    # Evidence with critical vulnerability that normally fails
    evidence = {
        "artifactDigest": DIGEST,
        "sbom": {"generatedBy": "syft", "location": "s3://sboms/hotfix.json"},
        "vulnerabilityScan": {
            "scanner": "trivy",
            "status": "failed",
            "critical": 1,
            "high": 0,
            "findings": ["CVE-2026-9999"],
        },
        "signature": {"provider": "cosign", "verified": True},
    }

    with db.transaction() as session:
        # Without break glass: denied
        denied = BuiltinPolicyEngine.evaluate_artifact_admission(
            session,
            evidence=evidence,
            expected_digest=DIGEST,
            require_evidence=True,
        )
        assert denied.allowed is False

        # Create and approve break glass
        req = BreakGlassService.create_request(
            session,
            target_type="artifact",
            target_id=DIGEST,
            requested_by="dev-lead@corp.example",
            reason="critical service restoration",
            incident_ticket="INC-1234",
        )
        BreakGlassService.approve_request(
            session,
            request_id=req.id,
            approved_by="sec-director@corp.example",
        )

        # Now evaluation permits override
        admitted = BuiltinPolicyEngine.evaluate_artifact_admission(
            session,
            evidence=evidence,
            expected_digest=DIGEST,
            require_evidence=True,
        )
        assert admitted.allowed is True
        assert "Emergency break-glass" in admitted.reason or "break-glass" in admitted.reason.lower()
        assert admitted.checks.get("break_glass") is not None


def _tokens_file(tmp_path, principals: list[dict]):
    import hashlib
    import secrets
    import yaml

    tokens: dict[str, str] = {}
    entries = []
    for principal in principals:
        token = f"tok-{principal['subject']}-{secrets.token_urlsafe(8)}"
        tokens[principal["subject"]] = token
        entries.append({**principal, "tokenSha256": hashlib.sha256(token.encode()).hexdigest()})
    path = tmp_path / "tokens.yaml"
    path.write_text(yaml.safe_dump({"principals": entries}), encoding="utf-8")
    return path, tokens


def test_break_glass_http_api_flow(tmp_path, monkeypatch):
    import importlib
    path, tokens = _tokens_file(
        tmp_path,
        [
            {"subject": "dana", "displayName": "Dana Developer", "roles": ["developer"]},
            {"subject": "raj", "displayName": "Raj Reviewer", "roles": ["reviewer"]},
        ],
    )
    monkeypatch.setenv("NETCI_AUTH_MODE", "token")
    monkeypatch.setenv("NETCI_AUTH_TOKENS_FILE", str(path))
    monkeypatch.setenv("NETCI_PIPELINE_API_KEY", "dummy-key-12345678")

    import app.main as main
    module = importlib.reload(main)
    client = TestClient(module.app)
    dana_headers = {"Authorization": f"Bearer {tokens['dana']}"}
    raj_headers = {"Authorization": f"Bearer {tokens['raj']}"}

    try:
        # 1. Dana (developer) creates break-glass request
        create_resp = client.post(
            "/break-glass/requests",
            headers=dana_headers,
            json={
                "targetType": "production_request",
                "targetId": "PR-2026",
                "reason": "Emergency DB migration failure rollback",
                "incidentTicket": "INC-777",
            },
        )
        assert create_resp.status_code == 201
        data = create_resp.json()
        req_id = data["id"]
        assert data["status"] == "pending"
        assert data["requestedBy"] == "dana"

        # 2. Dana tries to self-approve -> refused by dual-control (403 SEPARATION_OF_DUTIES)
        self_appr = client.post(
            f"/break-glass/requests/{req_id}/approve",
            headers=dana_headers,
            json={"ttlMinutes": 60},
        )
        assert self_appr.status_code == 403

        # 3. Raj (reviewer) approves -> 200 OK, status active
        reviewer_appr = client.post(
            f"/break-glass/requests/{req_id}/approve",
            headers=raj_headers,
            json={"ttlMinutes": 60},
        )
        assert reviewer_appr.status_code == 200
        appr_data = reviewer_appr.json()
        assert appr_data["status"] == "active"
        assert appr_data["approvedBy"] == "raj"

        # 4. Query active break glass
        active_resp = client.get(
            "/break-glass/active",
            headers=dana_headers,
            params={"targetType": "production_request", "targetId": "PR-2026"},
        )
        assert active_resp.status_code == 200
        assert active_resp.json()["id"] == req_id
    finally:
        monkeypatch.delenv("NETCI_AUTH_MODE", raising=False)
        monkeypatch.delenv("NETCI_AUTH_TOKENS_FILE", raising=False)
        importlib.reload(main)
