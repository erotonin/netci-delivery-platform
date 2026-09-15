"""The edge-agent channel is a remote execution path. These pin what closed it.

The WebSocket used to `accept()` unconditionally and take the hostname from the query
string, so anyone who could reach the API could register as any host. Execute then
matched hostnames by substring, so an agent named `a` received the commands meant for
every host containing an `a`. Both are gone; these tests are what keeps them gone.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import app.main as main
from app import workload_identity
from app.main import app
from app.workload_identity import Scope, Workload

client = TestClient(app)
KEYS = "k1:" + "w" * 48


@pytest.fixture(autouse=True)
def keys(monkeypatch):
    monkeypatch.setenv("NETCI_WORKLOAD_TOKEN_KEYS", KEYS)
    main._ACTIVE_RUNNERS.clear()
    main._ACTIVE_AGENT_INFO.clear()
    yield
    main._ACTIVE_RUNNERS.clear()
    main._ACTIVE_AGENT_INFO.clear()


def agent_token(hostname: str) -> str:
    return workload_identity.mint(
        workload=Workload.AGENT, application_id=None,
        scopes={Scope.AGENT_CONNECT}, agent_hostname=hostname,
    )


def test_an_unauthenticated_agent_is_refused_before_accept():
    with pytest.raises(WebSocketDisconnect) as closed:
        with client.websocket_connect("/api/v1/agents/ws?agent_id=rogue"):
            pass
    assert closed.value.code == 4403
    assert main._ACTIVE_RUNNERS == {}


def test_the_query_string_cannot_choose_the_hostname():
    """The hostname is the token's claim. A query parameter naming another host is ignored."""

    token = agent_token("edge-01")
    with client.websocket_connect(
        f"/api/v1/agents/ws?agent_id=x&hostname=prod-db-01&token={token}"
    ) as ws:
        ws.send_json({"type": "TELEMETRY_HEARTBEAT", "telemetry": {}})
        ws.receive_json()
        assert set(main._ACTIVE_RUNNERS) == {"edge-01"}
        assert "prod-db-01" not in main._ACTIVE_RUNNERS


def test_a_jenkins_token_cannot_register_as_an_agent():
    from uuid import uuid4

    token = workload_identity.mint(
        workload=Workload.JENKINS, application_id=uuid4(), pipeline_run_id=uuid4(),
        scopes={Scope.CI_RESULT},
    )
    with pytest.raises(WebSocketDisconnect) as closed:
        with client.websocket_connect(f"/api/v1/agents/ws?token={token}"):
            pass
    assert closed.value.code == 4403


def test_an_expired_agent_token_is_refused():
    import time

    token = workload_identity.mint(
        workload=Workload.AGENT, application_id=None, scopes={Scope.AGENT_CONNECT},
        agent_hostname="edge-01", ttl_seconds=60, now=int(time.time()) - 7200,
    )
    with pytest.raises(WebSocketDisconnect) as closed:
        with client.websocket_connect(f"/api/v1/agents/ws?token={token}"):
            pass
    assert closed.value.code == 4401


def test_execute_matches_the_hostname_exactly():
    """Substring matching let one agent answer for many. Exact or nothing."""

    token = agent_token("a")
    with client.websocket_connect(f"/api/v1/agents/ws?token={token}"):
        refused = client.post(
            "/api/v1/agents/execute",
            json={"hostname": "prod-app-01", "command": "uptime", "timeout": 5},
        )
    assert refused.status_code == 404
    assert refused.json()["code"] == "AGENT_NOT_CONNECTED"


def test_execute_is_platform_admin_only():
    from app.main import execute_agent_command
    from app.policy.rules import Role

    dependency = execute_agent_command.__wrapped__ if hasattr(execute_agent_command, "__wrapped__") else None
    # The dependency object is what FastAPI inspects; read the roles it was built with.
    import inspect
    params = inspect.signature(execute_agent_command).parameters
    roles = params["principal"].default.dependency.__closure__[0].cell_contents
    assert roles == frozenset({Role.PLATFORM_ADMIN})
    _ = dependency


def test_agent_token_minting_is_platform_admin_only_and_names_the_host():
    issued = client.post("/api/v1/agents/token", json={"hostname": "edge-02", "ttlSeconds": 600})
    assert issued.status_code == 201
    claims = workload_identity.verify(issued.json()["token"])
    assert claims.workload == Workload.AGENT
    assert claims.agent_hostname == "edge-02"
    assert not claims.permits(Scope.DEPLOYMENT_RESULT)
    assert not claims.permits(Scope.CI_RESULT)


def test_a_command_outside_the_allowlist_is_refused_before_reaching_the_agent():
    token = agent_token("edge-03")
    with client.websocket_connect(f"/api/v1/agents/ws?token={token}"):
        refused = client.post(
            "/api/v1/agents/execute",
            json={"hostname": "edge-03", "command": "rm -rf /", "timeout": 5},
        )
    assert refused.status_code == 400
    assert refused.json()["code"] == "COMMAND_POLICY_VIOLATION"
