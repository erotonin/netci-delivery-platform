"""Tests for Phase 11 Security Exceptions API and Dual Control."""

from __future__ import annotations

import hashlib
import importlib
import secrets
from datetime import datetime, timedelta, timezone

import pytest
import yaml
from fastapi.testclient import TestClient

DIGEST = "sha256:" + "e" * 64


def _tokens_file(tmp_path, principals: list[dict]):
    tokens: dict[str, str] = {}
    entries = []
    for principal in principals:
        token = f"tok-{principal['subject']}-{secrets.token_urlsafe(8)}"
        tokens[principal["subject"]] = token
        entries.append({**principal, "tokenSha256": hashlib.sha256(token.encode()).hexdigest()})
    path = tmp_path / "tokens.yaml"
    path.write_text(yaml.safe_dump({"principals": entries}), encoding="utf-8")
    return path, tokens


def test_security_exceptions_api_flow(tmp_path, monkeypatch):
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
        future = datetime.now(timezone.utc) + timedelta(days=14)
        past = datetime.now(timezone.utc) - timedelta(days=1)

        # 1. Reject expired/past date
        past_resp = client.post(
            "/security-exceptions",
            headers=raj_headers,
            json={
                "cve": "CVE-2026-1111",
                "artifactDigest": DIGEST,
                "owner": "dana",
                "reason": "upstream fix pending",
                "expiresAt": past.isoformat(),
            },
        )
        assert past_resp.status_code == 422
        assert "EXPIRED_DATE" in past_resp.text

        # 2. Reject self-approval: owner cannot approve own waiver
        self_resp = client.post(
            "/security-exceptions",
            headers=raj_headers,
            json={
                "cve": "CVE-2026-1111",
                "artifactDigest": DIGEST,
                "owner": "raj",  # Raj is the authenticated approver
                "reason": "upstream fix pending",
                "expiresAt": future.isoformat(),
            },
        )
        assert self_resp.status_code == 403
        assert "SEPARATION_OF_DUTIES" in self_resp.text

        # 3. Create valid exception: Raj approves for owner Dana
        create_resp = client.post(
            "/security-exceptions",
            headers=raj_headers,
            json={
                "cve": "CVE-2026-1111",
                "artifactDigest": DIGEST,
                "owner": "dana",
                "reason": "upstream fix pending in v2.4",
                "expiresAt": future.isoformat(),
            },
        )
        assert create_resp.status_code == 201
        created = create_resp.json()
        exc_id = created["id"]
        assert created["cve"] == "CVE-2026-1111"
        assert created["approvedBy"] == "raj"
        assert created["status"] == "active"

        # 4. List exceptions
        list_resp = client.get("/security-exceptions", headers=dana_headers, params={"activeOnly": True})
        assert list_resp.status_code == 200
        items = list_resp.json()
        assert any(i["id"] == exc_id for i in items)

        # 5. Revoke exception
        revoke_resp = client.post(f"/security-exceptions/{exc_id}/revoke", headers=raj_headers)
        assert revoke_resp.status_code == 200
        assert revoke_resp.json()["status"] == "revoked"

        # 6. List active exceptions now excludes revoked exception
        list_active_resp = client.get("/security-exceptions", headers=dana_headers, params={"activeOnly": True})
        assert list_active_resp.status_code == 200
        active_items = list_active_resp.json()
        assert not any(i["id"] == exc_id for i in active_items)

    finally:
        monkeypatch.delenv("NETCI_AUTH_MODE", raising=False)
        monkeypatch.delenv("NETCI_AUTH_TOKENS_FILE", raising=False)
        importlib.reload(main)
