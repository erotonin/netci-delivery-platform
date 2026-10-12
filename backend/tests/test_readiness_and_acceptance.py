import pytest
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


def test_acceptance_harness_without_a_stack_is_blocked_not_passed(tmp_path):
    """No API to talk to means nothing was verified. Every gate must say BLOCKED, and
    the rehearsal must write its evidence somewhere other than the real evidence dir."""
    cmd = [sys.executable, str(ROOT / "scripts" / "production_acceptance_harness.py")]
    env = {k: v for k, v in os.environ.items() if not k.startswith("NETCI_ACCEPTANCE_")}
    env["PYTHONPATH"] = f"{ROOT}/backend:{ROOT}"
    env["NETCI_ACCEPTANCE_EVIDENCE_DIR"] = str(tmp_path)
    result = subprocess.run(cmd, env=env, capture_output=True, text=True, cwd=ROOT)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Summary: 0 PASS, 0 FAIL, 10 BLOCKED" in result.stdout
    assert (tmp_path / "acceptance.xml").is_file()
    import json
    report = json.loads(next(tmp_path.glob("production_acceptance_*.json")).read_text())
    assert report["verdict"] == "BLOCKED"
    assert all(g["status"] == "BLOCKED" for g in report["gates"])


# ------------------------------------------------------------ cosign readiness


def _fake_cosign(directory: Path, version: str) -> Path:
    binary = directory / "cosign"
    binary.write_text(
        "#!/bin/sh\n"
        f'[ "$1" = version ] && printf \'{{"gitVersion":"{version}"}}\' && exit 0\n'
        "exit 1\n"
    )
    binary.chmod(0o755)
    return binary


def test_cosign_readiness_reports_the_configured_executable_not_path(tmp_path, monkeypatch):
    """The worker verifies with NETCI_COSIGN_EXECUTABLE; readiness must answer for that
    binary. Cosign 3 writes bundle signatures cosign 2 cannot read, so which binary and
    which version are the facts an operator needs when verification says
    "no signatures found"."""
    from app import readiness

    key = tmp_path / "cosign.pub"
    key.write_text("-----BEGIN PUBLIC KEY-----\nx\n-----END PUBLIC KEY-----\n")
    (tmp_path / "configured").mkdir()
    configured = _fake_cosign(tmp_path / "configured", "v3.1.2")
    monkeypatch.setenv("NETCI_SIGNATURE_VERIFY_MODE", "cosign")
    monkeypatch.setenv("NETCI_COSIGN_PUBLIC_KEY_FILE", str(key))
    monkeypatch.setenv("NETCI_COSIGN_EXECUTABLE", str(configured))
    monkeypatch.setenv("PATH", "/nonexistent")

    report = readiness.check_cosign()

    assert report["ready"] is True
    assert report["executable"] == str(configured)
    assert report["version"] == "v3.1.2"


def test_cosign_readiness_fails_when_the_configured_executable_is_missing(tmp_path, monkeypatch):
    from app import readiness

    key = tmp_path / "cosign.pub"
    key.write_text("k")
    monkeypatch.setenv("NETCI_SIGNATURE_VERIFY_MODE", "cosign")
    monkeypatch.setenv("NETCI_COSIGN_PUBLIC_KEY_FILE", str(key))
    monkeypatch.setenv("NETCI_COSIGN_EXECUTABLE", str(tmp_path / "missing-cosign"))

    report = readiness.check_cosign()

    assert report["ready"] is False
    assert "missing-cosign" in report["error"]


# ---------------------------------------------------------------- cd readiness


class _Orchestrator:
    def __init__(self, mode, address="127.0.0.1:1"):
        self.mode = mode
        self.address = address
        self.namespace = "default"
        self.task_queue = "netci-delivery"


def test_cd_readiness_without_temporal_is_optional_and_not_ready_claimed():
    from app import readiness

    report = readiness.check_cd(_Orchestrator("none"))
    assert report["status"] == "not_configured" and report["optional"] is True


def test_cd_readiness_reports_an_unreachable_temporal_as_not_ready():
    """"configured" used to count as ready. An address nobody answers must not."""
    from app import readiness

    report = readiness.check_cd(_Orchestrator("temporal", address="127.0.0.1:1"), timeout_seconds=1.5)
    assert report["ready"] is False
    assert report["status"] == "unreachable"
    assert report["address"] == "127.0.0.1:1"


def test_cd_readiness_refuses_a_queue_nobody_polls():
    from app import readiness

    base = {"mode": "temporal"}
    assert readiness.judge_cd_pollers(base, 0, None)["status"] == "no_workers"
    # Temporal still lists a poller that stopped asking for work a while ago.
    stale = readiness.judge_cd_pollers(base, 1, readiness.CD_POLLER_MAX_AGE_SECONDS + 1)
    assert stale["status"] == "no_workers" and stale["ready"] is False
    live = readiness.judge_cd_pollers(base, 2, 11.5)
    assert live["status"] == "ready" and live["ready"] is True
    assert live["pollers"] == 2 and live["youngestPollSecondsAgo"] == 11.5


# ------------------------------------------------------- periodic reconciliation


def test_reconcile_interval_is_read_from_the_environment(monkeypatch):
    from app import main as main_module

    monkeypatch.setenv("NETCI_RECONCILE_INTERVAL_SECONDS", "15")
    assert main_module._reconcile_interval_seconds() == 15.0
    monkeypatch.setenv("NETCI_RECONCILE_INTERVAL_SECONDS", "0")
    assert main_module._reconcile_interval_seconds() == 0.0
    monkeypatch.setenv("NETCI_RECONCILE_INTERVAL_SECONDS", "nonsense")
    assert main_module._reconcile_interval_seconds() == 60.0


@pytest.mark.asyncio
async def test_the_periodic_loop_calls_the_reconciler_and_survives_a_failing_pass(monkeypatch):
    """A lost Jenkins callback left a run `queued` forever on the live stack because
    nothing ever invoked the reconciler. The loop must call it, and one bad pass must
    not end it."""
    import asyncio
    from app import main as main_module

    calls: list[int] = []

    def fake_reconcile(**_):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("jenkins unreachable")
        return {"reconciledRuns": [{"pipelineRunId": "r1", "action": "reconciled_failed"}], "reconciledDeployments": []}

    monkeypatch.setenv("NETCI_RECONCILE_INTERVAL_SECONDS", "0.01")
    monkeypatch.setattr(main_module.reconciler, "reconcile", fake_reconcile)
    task = asyncio.create_task(main_module._reconcile_periodically())
    for _ in range(200):
        await asyncio.sleep(0.01)
        if len(calls) >= 3:
            break
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(calls) >= 3, "the loop stopped after the failing pass"


def test_readyz_does_not_hand_an_anonymous_caller_the_probe_failure_text(monkeypatch):
    """`/readyz` is unauthenticated and exempt from the rate limiter; a load balancer polls it.

    A probe failure's own text names the thing that failed -- the Jenkins URL, the DCIM
    endpoint, a secret file path. `/operator/health` exists for that detail and sits
    behind AdminAccess, so the split is the one the code already drew.
    """

    import json

    from app import readiness

    def exploding_probe(*args, **kwargs):
        return False, {
            "ci": {"mode": "jenkins", "status": "unavailable", "ready": False,
                   "error": "URLError",
                   "errorDetail": "<urlopen error [Errno 111] Connection refused> "
                                  "http://jenkins.internal.corp:8080/api/json"},
            "database": {"status": "ok"},
        }

    monkeypatch.setattr("app.main.probe_readiness", exploding_probe)

    client = TestClient(app)
    public = client.get("/readyz")
    assert public.status_code == 503
    assert "jenkins.internal.corp" not in public.text
    assert "errorDetail" not in public.text
    # The class of failure still reaches whoever is watching the endpoint.
    assert public.json()["ci"]["error"] == "URLError"

    # And nothing was lost: the operator view keeps the text.
    kept = readiness.without_operator_detail(json.loads(json.dumps(exploding_probe()[1])))
    assert "errorDetail" not in json.dumps(kept)
