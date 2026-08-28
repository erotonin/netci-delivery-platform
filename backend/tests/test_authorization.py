"""Who may do what, and where the identity in the audit trail comes from.

These tests exist because all three of these controls were once present in the codebase
in a form that did nothing: `require_environment_permission` was written and never
called, the approver was whatever string the caller put in the body, and every endpoint
except the CI callbacks was reachable without any credential at all. A control that is
not exercised by a test is a control that will quietly stop working.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import secrets
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


PIPELINE_KEY = "netci-local-pipeline-key"
MACHINE_HEADERS = {"Authorization": f"Bearer {PIPELINE_KEY}"}


def _tokens_file(tmp_path: Path, principals: list[dict]) -> tuple[Path, dict[str, str]]:
    """Write a token file and return it with the plaintext tokens keyed by subject."""

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


@pytest.fixture
def token_app(tmp_path, monkeypatch):
    """A client running in NETCI_AUTH_MODE=token with four distinct identities.

    The app is re-imported because the authenticator is built once at the composition
    root, which is the point: the mode is a deployment decision, not a per-request one.
    """

    path, tokens = _tokens_file(
        tmp_path,
        [
            {"subject": "vera", "displayName": "Vera Viewer", "roles": ["viewer"]},
            {"subject": "dana", "displayName": "Dana Developer", "roles": ["developer"]},
            {"subject": "raj", "displayName": "Raj Reviewer", "roles": ["reviewer"]},
            {"subject": "pat", "displayName": "Pat Platform", "roles": ["platform-admin"]},
        ],
    )
    monkeypatch.setenv("NETCI_AUTH_MODE", "token")
    monkeypatch.setenv("NETCI_AUTH_TOKENS_FILE", str(path))
    monkeypatch.setenv("NETCI_PIPELINE_API_KEY", PIPELINE_KEY)

    import app.main as main

    module = importlib.reload(main)
    client = TestClient(module.app)
    headers = {name: {"Authorization": f"Bearer {token}"} for name, token in tokens.items()}
    yield client, headers, path

    # Put the module back the way the rest of the suite expects to find it.
    monkeypatch.delenv("NETCI_AUTH_MODE", raising=False)
    monkeypatch.delenv("NETCI_AUTH_TOKENS_FILE", raising=False)
    importlib.reload(main)


def _application(client, headers) -> dict:
    response = client.post(
        "/applications",
        headers=headers,
        json={
            "name": f"auth-app-{secrets.token_hex(4)}",
            "repositoryUrl": "https://git.example.com/team/app",
            "pipelineTemplate": "container-ci-cd-v1",
            "runtime": "docker",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


# ------------------------------------------------------------------ authentication


def test_an_unauthenticated_caller_cannot_create_or_approve_anything(token_app):
    client, headers, _ = token_app

    for method, path, payload in (
        ("post", "/applications", {"name": "nope", "repositoryUrl": "https://x.example/y", "pipelineTemplate": "container-ci-cd-v1", "runtime": "docker"}),
        ("post", "/systems", {"name": "nope"}),
        ("get", "/applications", None),
        ("get", "/production-requests", None),
    ):
        response = getattr(client, method)(path, json=payload) if payload else getattr(client, method)(path)
        assert response.status_code == 401, f"{method} {path} answered {response.status_code}"
        assert response.headers.get("WWW-Authenticate") == "Bearer"


def test_healthz_stays_open_and_reports_the_auth_mode(token_app):
    client, _, _ = token_app
    response = client.get("/healthz")
    assert response.status_code in {200, 503}
    assert response.json()["engines"]["auth"] == "token"


def test_a_revoked_token_stops_working_without_a_restart(token_app):
    client, headers, path = token_app
    assert client.get("/applications", headers=headers["dana"]).status_code == 200

    import yaml

    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    document["principals"] = [item for item in document["principals"] if item["subject"] != "dana"]
    # mtime has one-second resolution on some filesystems; make the change detectable.
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    os.utime(path, (time.time() + 2, time.time() + 2))

    assert client.get("/applications", headers=headers["dana"]).status_code == 401
    assert client.get("/applications", headers=headers["raj"]).status_code == 200


def test_tokens_are_stored_as_hashes_not_as_secrets(token_app):
    _, headers, path = token_app
    contents = path.read_text(encoding="utf-8")
    for header in headers.values():
        token = header["Authorization"].removeprefix("Bearer ")
        assert token not in contents, "the token file must not contain a usable credential"


# ------------------------------------------------------------------- authorization


def test_a_viewer_can_read_but_not_change_anything(token_app):
    client, headers, _ = token_app
    assert client.get("/applications", headers=headers["vera"]).status_code == 200

    response = client.post(
        "/applications",
        headers=headers["vera"],
        json={"name": "viewer-app", "repositoryUrl": "https://git.example.com/a/b", "pipelineTemplate": "container-ci-cd-v1", "runtime": "docker"},
    )
    assert response.status_code == 403
    assert response.json()["code"] == "FORBIDDEN"
    # The message has to say what is missing, or the user cannot act on it.
    assert "developer" in response.json()["message"]


def test_a_developer_cannot_run_a_production_pipeline(token_app):
    """`require_environment_permission` used to exist and never run. This is its test."""

    client, headers, _ = token_app
    application = _application(client, headers["dana"])

    allowed = client.post(
        f"/applications/{application['id']}/pipeline-runs",
        headers=headers["dana"],
        json={"commitSha": "abcdef1234567", "environment": "staging"},
    )
    assert allowed.status_code == 202

    refused = client.post(
        f"/applications/{application['id']}/pipeline-runs",
        headers=headers["dana"],
        json={"commitSha": "abcdef1234567", "environment": "prod"},
    )
    assert refused.status_code == 403
    assert refused.json()["code"] == "ENVIRONMENT_FORBIDDEN"


def test_a_reviewer_may_run_a_production_pipeline(token_app):
    client, headers, _ = token_app
    application = _application(client, headers["pat"])
    response = client.post(
        f"/applications/{application['id']}/pipeline-runs",
        headers=headers["raj"],
        json={"commitSha": "abcdef1234567", "environment": "prod"},
    )
    assert response.status_code == 202


# --------------------------------------------------------------- machine callbacks


def test_the_pipeline_key_cannot_approve_and_a_human_cannot_forge_a_build_result(token_app):
    client, headers, _ = token_app
    application = _application(client, headers["pat"])
    run = client.post(
        f"/applications/{application['id']}/pipeline-runs",
        headers=headers["pat"],
        json={"commitSha": "abcdef1234567", "environment": "prod"},
    ).json()

    # A person -- even a platform admin -- may not post a build result.
    forged = client.post(
        f"/pipeline-runs/{run['id']}/ci-result",
        headers=headers["pat"],
        json={"status": "succeeded", "artifactDigest": f"sha256:{'c' * 64}"},
    )
    assert forged.status_code == 403

    client.post(f"/pipeline-runs/{run['id']}/ci-result", headers=MACHINE_HEADERS, json={"status": "running"})
    completed = client.post(
        f"/pipeline-runs/{run['id']}/ci-result",
        headers=MACHINE_HEADERS,
        json={"status": "succeeded", "artifactDigest": f"sha256:{'c' * 64}"},
    ).json()
    deployment_id = completed["deployment"]["id"]

    # ...and the machine key may not approve the deployment it just created.
    assert client.post(f"/deployments/{deployment_id}/approve", headers=MACHINE_HEADERS, json={}).status_code == 403


# ------------------------------------------------------- identity and separation


def test_the_approver_is_the_credential_holder_not_the_request_body(token_app):
    client, headers, _ = token_app
    application = _application(client, headers["pat"])
    run = client.post(
        f"/applications/{application['id']}/pipeline-runs",
        headers=headers["pat"],
        json={"commitSha": "abcdef1234567", "environment": "prod"},
    ).json()
    assert run["startedBy"] == "pat"

    client.post(f"/pipeline-runs/{run['id']}/ci-result", headers=MACHINE_HEADERS, json={"status": "running"})
    completed = client.post(
        f"/pipeline-runs/{run['id']}/ci-result",
        headers=MACHINE_HEADERS,
        json={"status": "succeeded", "artifactDigest": f"sha256:{'d' * 64}"},
    ).json()

    approved = client.post(
        f"/deployments/{completed['deployment']['id']}/approve",
        headers=headers["raj"],
        json={"comment": "shipping it", "actor": "somebody-else"},
    )
    assert approved.status_code == 202
    assert approved.json()["approvedBy"] == "raj"


def test_the_person_who_started_a_production_run_cannot_approve_it(token_app):
    client, headers, _ = token_app
    application = _application(client, headers["pat"])
    run = client.post(
        f"/applications/{application['id']}/pipeline-runs",
        headers=headers["raj"],
        json={"commitSha": "abcdef1234567", "environment": "prod"},
    ).json()
    client.post(f"/pipeline-runs/{run['id']}/ci-result", headers=MACHINE_HEADERS, json={"status": "running"})
    completed = client.post(
        f"/pipeline-runs/{run['id']}/ci-result",
        headers=MACHINE_HEADERS,
        json={"status": "succeeded", "artifactDigest": f"sha256:{'e' * 64}"},
    ).json()
    deployment_id = completed["deployment"]["id"]

    same_person = client.post(f"/deployments/{deployment_id}/approve", headers=headers["raj"], json={})
    assert same_person.status_code == 403
    assert same_person.json()["code"] == "SEPARATION_OF_DUTIES"

    second_person = client.post(f"/deployments/{deployment_id}/approve", headers=headers["pat"], json={})
    assert second_person.status_code == 202
    assert second_person.json()["approvedBy"] == "pat"


def test_me_reports_the_caller_back_to_the_portal(token_app):
    client, headers, _ = token_app
    body = client.get("/me", headers=headers["raj"]).json()
    assert body["principal"]["subject"] == "raj"
    assert body["principal"]["displayName"] == "Raj Reviewer"
    assert body["principal"]["roles"] == ["reviewer"]
    assert body["authMode"] == "token"
    assert body["separationOfDuties"] is True


# --------------------------------------------------------------- the open-mode guard


def test_open_mode_serves_loopback_only():
    """NETCI_AUTH_MODE=none must not become a network-reachable open API by accident."""

    from app.main import LOOPBACK_HOSTS, current_principal
    from app.auth import OpenAuthenticator
    from fastapi import HTTPException

    class _Remote:
        host = "10.1.2.3"

    class _Request:
        client = _Remote()

    assert OpenAuthenticator().mode == "none"
    assert "testclient" in LOOPBACK_HOSTS  # so the rest of the suite still runs
    with pytest.raises(HTTPException) as raised:
        current_principal(_Request(), authorization=None)
    assert raised.value.status_code == 403
    assert raised.value.detail["code"] == "AUTH_NOT_CONFIGURED"
