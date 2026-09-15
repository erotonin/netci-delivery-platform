import hashlib
import hmac
import json
import os
from uuid import UUID, uuid4
from fastapi.testclient import TestClient
import pytest

import app.main as main_mod
from app.auth import Principal, Role
from app.domain.models import (
    Application,
    Environment,
    Runtime,
    ScmCommitStatus,
    ScmIntegration,
    ScmProviderType,
)
from app.adapters.scm import (
    GitHubScmProvider,
    GitLabScmProvider,
    MockScmProvider,
    get_scm_provider,
    set_scm_provider,
)

class _ClientProxy:
    def __getattr__(self, name: str):
        return getattr(TestClient(main_mod.app), name)

client = _ClientProxy()

class _PlatformProxy:
    def __getattr__(self, name: str):
        return getattr(main_mod.platform, name)

platform = _PlatformProxy()

ADMIN_PRINCIPAL = Principal(
    subject="admin-tester",
    display_name="Admin Tester",
    email="admin@example.com",
    roles=frozenset({Role.PLATFORM_ADMIN, Role.DEVELOPER}),
    method="token",
    teams=frozenset({"core-platform"}),
)


@pytest.fixture(autouse=True)
def setup_auth_and_providers():
    main_mod.app.dependency_overrides[main_mod.current_principal] = lambda: ADMIN_PRINCIPAL
    orig_github = get_scm_provider(ScmProviderType.GITHUB)
    orig_gitlab = get_scm_provider(ScmProviderType.GITLAB)
    mock_gh = MockScmProvider(ScmProviderType.GITHUB)
    mock_gl = MockScmProvider(ScmProviderType.GITLAB)
    set_scm_provider(ScmProviderType.GITHUB, mock_gh)
    set_scm_provider(ScmProviderType.GITLAB, mock_gl)
    yield
    set_scm_provider(ScmProviderType.GITHUB, orig_github)
    set_scm_provider(ScmProviderType.GITLAB, orig_gitlab)
    main_mod.app.dependency_overrides.pop(main_mod.current_principal, None)


def create_test_application(name: str | None = None) -> Application:
    app_name = name or f"scm-app-{uuid4().hex[:6]}"
    return main_mod.platform.create_application(
        name=app_name,
        repository_url=f"https://github.com/org/{app_name}",
        pipeline_template="container-ci-cd-v1",
        runtime=Runtime.DOCKER,
        default_environment=Environment.DEV,
        stages=("checkout", "unit-test", "build", "sbom", "vulnerability-scan", "sign", "publish"),
        owner_team="core-platform",
        idempotency_key=str(uuid4()),
    )


def test_github_webhook_signature_verification_and_trigger():
    app_obj = create_test_application()
    repo_name = f"org/repo-{uuid4().hex[:6]}"
    secret = "super-secret-token-12345"

    # Configure SCM integration
    scm_res = client.post(
        f"/applications/{app_obj.id}/scm",
        json={
            "provider": "github",
            "repositoryIdentity": repo_name,
            "secretToken": secret,
            "credentialReference": "git-deploy-key-id",
        },
    )
    assert scm_res.status_code == 201
    scm_data = scm_res.json()
    assert scm_data["repositoryIdentity"] == repo_name
    assert "secretToken" not in scm_data  # Secret redacted!

    payload = {
        "repository": {"full_name": repo_name},
        "after": "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2",
        "ref": "refs/heads/main",
        "sender": {"login": "octocat"},
    }
    raw_body = json.dumps(payload).encode("utf-8")
    delivery_id = f"delivery-{uuid4().hex}"

    # 1. Invalid signature -> 401
    bad_headers = {
        "x-github-delivery": delivery_id,
        "x-github-event": "push",
        "x-hub-signature-256": "sha256=invalid-signature",
        "content-type": "application/json",
    }
    res_bad = client.post("/webhooks/scm/github", content=raw_body, headers=bad_headers)
    assert res_bad.status_code == 401

    # 2. Valid signature -> 201
    valid_sig = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    good_headers = {
        "x-github-delivery": delivery_id,
        "x-github-event": "push",
        "x-hub-signature-256": f"sha256={valid_sig}",
        "content-type": "application/json",
    }
    res_good = client.post("/webhooks/scm/github", content=raw_body, headers=good_headers)
    assert res_good.status_code == 201
    data = res_good.json()
    assert data["status"] == "triggered"
    assert data["deliveryId"] == delivery_id
    assert "pipelineRunId" in data

    # Verify pipeline was started with server-managed credentialsId
    run = platform.get_pipeline(UUID(data["pipelineRunId"]))
    assert run.commit_sha == "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2"
    assert run.branch == "main"
    assert run.parameters.get("credentialsId") == "git-deploy-key-id"


def test_gitlab_webhook_token_verification_and_trigger():
    app_obj = create_test_application()
    repo_name = f"gitlab-org/project-{uuid4().hex[:6]}"
    secret = "gitlab-secret-token-9999"

    scm_res = client.post(
        f"/applications/{app_obj.id}/scm",
        json={
            "provider": "gitlab",
            "repositoryIdentity": repo_name,
            "secretToken": secret,
            "credentialReference": "gitlab-token-ref",
        },
    )
    assert scm_res.status_code == 201

    payload = {
        "project": {"path_with_namespace": repo_name},
        "checkout_sha": "f1e2d3c4b5a6f1e2d3c4b5a6f1e2d3c4b5a6f1e2",
        "ref": "refs/heads/release/v1.0",
        "user_username": "dev-user",
    }
    raw_body = json.dumps(payload).encode("utf-8")
    delivery_id = f"gl-delivery-{uuid4().hex}"

    # Invalid token -> 401
    bad_headers = {
        "x-gitlab-delivery": delivery_id,
        "x-gitlab-event": "Push Hook",
        "x-gitlab-token": "wrong-token",
        "content-type": "application/json",
    }
    res_bad = client.post("/webhooks/scm/gitlab", content=raw_body, headers=bad_headers)
    assert res_bad.status_code == 401

    # Valid token -> 201
    good_headers = {
        "x-gitlab-delivery": delivery_id,
        "x-gitlab-event": "Push Hook",
        "x-gitlab-token": secret,
        "content-type": "application/json",
    }
    res_good = client.post("/webhooks/scm/gitlab", content=raw_body, headers=good_headers)
    assert res_good.status_code == 201
    data = res_good.json()
    assert data["status"] == "triggered"
    assert data["deliveryId"] == delivery_id


def test_webhook_replay_delivery_id_deduplication():
    app_obj = create_test_application()
    repo_name = f"org/dedup-repo-{uuid4().hex[:6]}"
    secret = "dedup-secret-token-5555"

    client.post(
        f"/applications/{app_obj.id}/scm",
        json={
            "provider": "github",
            "repositoryIdentity": repo_name,
            "secretToken": secret,
        },
    )

    payload = {
        "repository": {"full_name": repo_name},
        "after": "1111222233334444555566667777888899990000",
        "ref": "refs/heads/main",
        "sender": {"login": "ci-bot"},
    }
    raw_body = json.dumps(payload).encode("utf-8")
    delivery_id = f"fixed-delivery-{uuid4().hex}"
    valid_sig = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()

    headers = {
        "x-github-delivery": delivery_id,
        "x-github-event": "push",
        "x-hub-signature-256": f"sha256={valid_sig}",
        "content-type": "application/json",
    }

    # First delivery: accepted and triggered (201)
    res1 = client.post("/webhooks/scm/github", content=raw_body, headers=headers)
    assert res1.status_code == 201
    assert res1.json()["status"] == "triggered"

    # Replayed delivery: atomically detected as duplicate, returns 200 without re-triggering
    res2 = client.post("/webhooks/scm/github", content=raw_body, headers=headers)
    assert res2.status_code == 200
    assert res2.json()["status"] == "ignored_duplicate"
    assert res2.json()["deliveryId"] == delivery_id


def test_repository_a_does_not_trigger_application_b():
    app_a = create_test_application(f"service-alpha-{uuid4().hex[:6]}")
    app_b = create_test_application(f"service-beta-{uuid4().hex[:6]}")
    secret = "isolation-secret-token-777"

    repo_a = f"org/repo-alpha-{uuid4().hex[:6]}"
    repo_b = f"org/repo-beta-{uuid4().hex[:6]}"

    client.post(
        f"/applications/{app_a.id}/scm",
        json={"provider": "github", "repositoryIdentity": repo_a, "secretToken": secret},
    )
    client.post(
        f"/applications/{app_b.id}/scm",
        json={"provider": "github", "repositoryIdentity": repo_b, "secretToken": secret},
    )

    # Trigger webhook for repo_a
    payload_a = {
        "repository": {"full_name": repo_a},
        "after": "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
        "ref": "refs/heads/main",
        "sender": {"login": "developer"},
    }
    raw_a = json.dumps(payload_a).encode("utf-8")
    delivery_id = f"del-a-{uuid4().hex}"
    sig_a = hmac.new(secret.encode("utf-8"), raw_a, hashlib.sha256).hexdigest()

    res = client.post(
        "/webhooks/scm/github",
        content=raw_a,
        headers={
            "x-github-delivery": delivery_id,
            "x-github-event": "push",
            "x-hub-signature-256": f"sha256={sig_a}",
            "content-type": "application/json",
        },
    )
    assert res.status_code == 201
    run_id = res.json()["pipelineRunId"]

    # Verify run belongs to app_a, never app_b
    run = platform.get_pipeline(UUID(run_id))
    assert run.application_id == app_a.id
    assert run.application_id != app_b.id

    # Webhook for unmapped repository returns 404
    unmapped_payload = {
        "repository": {"full_name": "org/unmapped-repo"},
        "after": "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
        "ref": "refs/heads/main",
    }
    raw_unmapped = json.dumps(unmapped_payload).encode("utf-8")
    res_unmapped = client.post(
        "/webhooks/scm/github",
        content=raw_unmapped,
        headers={
            "x-github-delivery": f"del-{uuid4().hex}",
            "x-github-event": "push",
            "x-hub-signature-256": f"sha256={sig_a}",
            "content-type": "application/json",
        },
    )
    assert res_unmapped.status_code == 404


def test_secret_redaction_in_api_and_audit():
    app_obj = create_test_application()
    repo_name = f"org/secret-repo-{uuid4().hex[:6]}"
    secret = "confidential-token-do-not-leak"

    client.post(
        f"/applications/{app_obj.id}/scm",
        json={
            "provider": "github",
            "repositoryIdentity": repo_name,
            "secretToken": secret,
            "credentialReference": "git-ssh-key",
        },
    )

    # 1. GET /applications/{id}/scm
    res = client.get(f"/applications/{app_obj.id}/scm")
    assert res.status_code == 200
    data = res.json()
    assert data["repositoryIdentity"] == repo_name
    assert data["credentialReference"] == "git-ssh-key"
    assert "secretToken" not in data
    assert "secret_token" not in data
    assert "secretTokenHash" not in data
    assert "secret_token_hash" not in data

    # 2. Check audit logs
    audit_events = platform.audit_records({app_obj.id})
    for record in audit_events:
        payload_str = json.dumps(record.payload or {})
        assert secret not in payload_str


def test_payload_size_limit():
    large_payload = b"x" * (1024 * 1024 + 10)  # > 1MB
    res = client.post(
        "/webhooks/scm/github",
        content=large_payload,
        headers={"content-type": "application/json"},
    )
    assert res.status_code == 413


def test_commit_status_lifecycle():
    app_obj = create_test_application()
    repo_name = f"org/status-repo-{uuid4().hex[:6]}"
    secret = "status-secret-token-123"

    client.post(
        f"/applications/{app_obj.id}/scm",
        json={
            "provider": "github",
            "repositoryIdentity": repo_name,
            "secretToken": secret,
        },
    )

    mock_provider = get_scm_provider(ScmProviderType.GITHUB)
    assert isinstance(mock_provider, MockScmProvider)
    mock_provider.status_updates.clear()

    # Start a pipeline run
    run = platform.start_pipeline(
        app_obj.id,
        commit_sha="c0ffee11c0ffee22c0ffee33c0ffee44c0ffee55",
        branch="main",
        environment=Environment.DEV,
        parameters={},
        correlation_id=str(uuid4()),
        idempotency_key=str(uuid4()),
    )
    # Status PENDING should have been recorded
    assert any(
        s["commit_sha"] == "c0ffee11c0ffee22c0ffee33c0ffee44c0ffee55"
        and s["status"] == ScmCommitStatus.PENDING
        for s in mock_provider.status_updates
    )

    # Transition to RUNNING
    platform.record_ci_result(
        run.id,
        result_status="running",
        artifact_digest=None,
        log_lines=["Compiling..."],
    )
    assert any(
        s["commit_sha"] == "c0ffee11c0ffee22c0ffee33c0ffee44c0ffee55"
        and s["status"] == ScmCommitStatus.RUNNING
        for s in mock_provider.status_updates
    )

    # Transition to SUCCEEDED
    artifact = f"sha256:{hashlib.sha256(b'img').hexdigest()}"
    platform.record_ci_result(
        run.id,
        result_status="succeeded",
        artifact_digest=artifact,
        log_lines=["Build done"],
    )
    assert any(
        s["commit_sha"] == "c0ffee11c0ffee22c0ffee33c0ffee44c0ffee55"
        and s["status"] == ScmCommitStatus.SUCCESS
        for s in mock_provider.status_updates
    )
