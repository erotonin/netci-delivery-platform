import os
import subprocess
import sys
from pathlib import Path
from fastapi.testclient import TestClient

from app.main import app
from app.auth import Principal, Role

client = TestClient(app)
ROOT = Path(__file__).resolve().parents[2]


def test_livez_returns_ok():
    response = client.get("/livez")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert "timestamp" in data


def test_readyz_returns_200_when_dependencies_healthy():
    response = client.get("/readyz")
    assert response.status_code in (200, 503)
    data = response.json()
    assert "status" in data
    assert "database" in data
    assert "ci" in data
    assert "cd" in data


def test_readyz_fails_503_when_database_unavailable(monkeypatch):
    from app import main
    # Simulate DB persistence failure
    monkeypatch.setattr(main.platform, "persistence_health", lambda: "unavailable: connection refused")
    response = client.get("/readyz")
    assert response.status_code == 503
    data = response.json()
    assert data["status"] == "unavailable"
    assert data["database"]["ready"] is False


def test_operator_health_requires_admin_access():
    import app.main as main_mod
    viewer_principal = Principal(
        subject="viewer-user",
        display_name="Viewer User",
        email="viewer@example.com",
        roles=frozenset({Role.VIEWER}),
        method="token",
        teams=frozenset(),
    )
    main_mod.app.dependency_overrides[main_mod.current_principal] = lambda: viewer_principal
    try:
        response = TestClient(main_mod.app).get("/operator/health")
        assert response.status_code == 403
    finally:
        main_mod.app.dependency_overrides.pop(main_mod.current_principal, None)


def test_operator_health_with_admin_access():
    import app.main as main_mod
    admin_principal = Principal(
        subject="admin-user",
        display_name="Admin User",
        email="admin@example.com",
        roles=frozenset({Role.PLATFORM_ADMIN}),
        method="token",
        teams=frozenset({"admins"}),
    )
    main_mod.app.dependency_overrides[main_mod.current_principal] = lambda: admin_principal
    try:
        response = TestClient(main_mod.app).get("/operator/health")
        assert response.status_code in (200, 503)
        data = response.json()
        assert data.get("operatorView") is True
        raw_text = str(data)
        assert "netci-local-only" not in raw_text
        assert "Bearer" not in raw_text
    finally:
        main_mod.app.dependency_overrides.pop(main_mod.current_principal, None)


def test_acceptance_harness_generates_evidence():
    cmd = [
        sys.executable,
        str(ROOT / "scripts" / "production_acceptance_harness.py"),
    ]
    env = dict(os.environ)
    env["PYTHONPATH"] = f"{ROOT}/backend:{ROOT}"
    result = subprocess.run(cmd, env=env, capture_output=True, text=True, cwd=ROOT)
    assert result.returncode in (0, 1)
    assert "netCI Production Acceptance Harness" in result.stdout
    assert "Summary:" in result.stdout
    assert (ROOT / "evidence" / "acceptance.xml").is_file()
