"""Coding agents are principals of their own kind (ADR-052).

The server decides who is an agent -- from the identity provider's groups or the OAuth
client that got the token, or the token file -- and an agent can never hold a role that
approves, cannot break glass, cannot move an SCM integration, and waits behind people.
"""

from __future__ import annotations

import os
import uuid

import pytest
from fastapi.testclient import TestClient

import app.main as main_mod
from app.auth import AGENT, HUMAN, AuthError, Principal, TokenAuthenticator
from app.delivery import DeliveryPlatform
from app.domain.models import Environment, Runtime
from app.policy.rules import Role
from app.store.memory import InMemoryDatabase
from app.store.records import ResourceQuotaRecord

from test_auth_oidc import authenticator, bearer, claims, ec_key, rsa_key, sign_rs256  # noqa: F401
from test_break_glass import _tokens_file


# ------------------------------------------------------------------ who is an agent


def test_an_oidc_member_of_an_agent_group_is_an_agent(rsa_key, ec_key, monkeypatch):
    verifier = authenticator(rsa_key, ec_key, monkeypatch, agent_groups=frozenset({"coding-agents"}))
    agent = verifier.authenticate(bearer(sign_rs256(rsa_key, claims(roles=["engineers", "coding-agents"]))))
    person = verifier.authenticate(bearer(sign_rs256(rsa_key, claims())))
    assert (agent.kind, person.kind) == (AGENT, HUMAN)
    assert agent.as_json()["kind"] == "agent"


def test_a_token_issued_to_an_agent_client_is_an_agent(rsa_key, ec_key, monkeypatch):
    verifier = authenticator(rsa_key, ec_key, monkeypatch, agent_clients=frozenset({"claude-code-ci"}))
    principal = verifier.authenticate(bearer(sign_rs256(rsa_key, claims(azp="claude-code-ci"))))
    assert principal.is_agent


def test_an_agent_that_the_idp_maps_to_reviewer_is_refused_not_quietly_downgraded(rsa_key, ec_key, monkeypatch):
    verifier = authenticator(rsa_key, ec_key, monkeypatch, agent_groups=frozenset({"coding-agents"}))
    with pytest.raises(AuthError) as refused:
        verifier.authenticate(bearer(sign_rs256(rsa_key, claims(roles=["release-managers", "coding-agents"]))))
    assert refused.value.code == "AGENT_ROLE_NOT_ALLOWED" and refused.value.status == 403


def test_the_token_file_declares_agents_and_refuses_an_agent_reviewer(tmp_path):
    path, tokens = _tokens_file(tmp_path, [{"subject": "bot", "roles": ["developer"], "kind": "agent"}])
    assert TokenAuthenticator(path).authenticate(f"Bearer {tokens['bot']}").is_agent

    bad, bad_tokens = _tokens_file(tmp_path, [{"subject": "bot2", "roles": ["reviewer"], "kind": "agent"}])
    with pytest.raises(AuthError) as refused:
        TokenAuthenticator(bad).authenticate(f"Bearer {bad_tokens['bot2']}")
    assert refused.value.status == 503 and "may not hold reviewer" in refused.value.message


def test_an_unknown_kind_in_the_token_file_is_a_configuration_error(tmp_path):
    path, tokens = _tokens_file(tmp_path, [{"subject": "x", "roles": ["developer"], "kind": "robot"}])
    with pytest.raises(AuthError) as refused:
        TokenAuthenticator(path).authenticate(f"Bearer {tokens['x']}")
    assert refused.value.code == "AUTH_NOT_CONFIGURED"


# ------------------------------------------------------------------ what an agent may not do

AGENT_DEV = Principal(subject="agent-7", display_name="Agent 7", email="", roles=frozenset({Role.DEVELOPER}),
                      method="oidc", kind=AGENT)


@pytest.fixture()
def as_agent():
    client = TestClient(main_mod.app)
    main_mod.platform.reset()
    main_mod.app.dependency_overrides[main_mod.current_principal] = lambda: AGENT_DEV
    yield client
    main_mod.app.dependency_overrides.pop(main_mod.current_principal, None)


def test_an_agent_cannot_request_break_glass(as_agent):
    response = as_agent.post("/break-glass/requests", json={
        "targetType": "artifact", "targetId": "sha256:" + "a" * 64,
        "reason": "let me through", "incidentTicket": "INC-1"})
    assert response.status_code == 403 and response.json()["detail"]["code"] == "AGENT_MAY_NOT_BREAK_GLASS"


def test_an_agent_cannot_move_an_scm_integration(as_agent):
    response = as_agent.post(f"/applications/{uuid.uuid4()}/scm", json={
        "provider": "github", "repositoryIdentity": "agent/own-repo", "secretToken": "s" * 24})
    assert response.status_code == 403 and response.json()["detail"]["code"] == "AGENT_MAY_NOT_CONFIGURE_SCM"


def test_an_agent_cannot_delete_a_system(as_agent):
    response = as_agent.delete("/systems/anything")
    assert response.status_code == 403 and response.json()["detail"]["code"] == "AGENT_MAY_NOT_DELETE_SYSTEMS"


# ------------------------------------------------------------------ people go first


DATABASE_URL = os.getenv("NETCI_TEST_DATABASE_URL", "").strip()


@pytest.mark.parametrize("store", ["memory", pytest.param("postgres", marks=pytest.mark.skipif(
    not DATABASE_URL, reason="set NETCI_TEST_DATABASE_URL to check the PostgreSQL ordering"))])
def test_waiting_runs_of_people_are_admitted_before_older_ones_of_agents(store, monkeypatch):
    if store == "postgres":
        from test_persistence_postgres import truncate

        monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
        truncate()
        platform = DeliveryPlatform()
    else:
        platform = DeliveryPlatform(database=InMemoryDatabase())
    application = platform.create_application(
        name=f"lane-{uuid.uuid4().hex[:6]}", repository_url="https://git.example/lane",
        pipeline_template="container-ci-cd-v1", runtime=Runtime.DOCKER, default_environment=Environment.DEV,
        stages=[], idempotency_key=uuid.uuid4().hex)
    with platform.transaction() as tx:
        tx.set_resource_quota(ResourceQuotaRecord(id=uuid.uuid4(), scope="application", scope_id=str(application.id),
                                                  max_concurrent_pipelines=1))

    def start(kind, branch):
        return platform.start_pipeline(
            application.id, commit_sha="a" * 40, branch=branch, environment=Environment.DEV, parameters={},
            correlation_id="c", idempotency_key=uuid.uuid4().hex,
            trigger={"event": "push", "branch": branch, "actorKind": kind})

    first = start("human", "main")
    agent_runs = [start("agent", f"agent/{i}") for i in range(3)]
    person = start("human", "feature/x")
    platform.record_ci_result(first.id, "running", None, [])
    platform.record_ci_result(first.id, "failed", None, [])

    assert platform.get_pipeline(person.id).admitted_at is not None
    assert all(platform.get_pipeline(run.id).admitted_at is None for run in agent_runs)
    if store == "postgres":
        truncate()


def test_the_webhook_marks_a_listed_scm_login_as_an_agent(monkeypatch):
    from test_ci_cd_separation import _module, _push, fresh_platform  # noqa: F401

    monkeypatch.setenv("NETCI_AGENT_SCM_LOGINS", "dev1")
    main_mod.platform.reset()
    module, repo = _module("lane-api")
    started = _push(repo, "refs/heads/feature/y")
    assert started.status_code == 201, started.text
    run = main_mod.platform.get_pipeline(uuid.UUID(started.json()["pipelineRunId"]))
    assert run.trigger["actorKind"] == "agent"
