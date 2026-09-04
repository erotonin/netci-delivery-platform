"""What a real callback token still may not do.

Verifying a signature proves a token was minted by netCI. These tests cover the part
after that: a genuine token for run A must not write to run B, a Jenkins controller must
not report a deployment outcome, and a terminal token must not be replayable. Under the
shared pipeline key every one of these was allowed.
"""

from __future__ import annotations

import time
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app import workload_identity
from app.main import app
from app.workload_identity import Scope, Workload

client = TestClient(app)
KEYS = "k1:" + "z" * 48
DIGEST = "sha256:" + "a" * 64


@pytest.fixture(autouse=True)
def workload_keys(monkeypatch):
    monkeypatch.setenv("NETCI_WORKLOAD_TOKEN_KEYS", KEYS)
    monkeypatch.delenv("NETCI_WORKLOAD_TOKEN_KEYS_FILE", raising=False)
    main.platform.reset()
    main.portal.reset()


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def start_run(name: str = "cb-app", environment: str = "staging"):
    created = client.post("/applications", json={
        "name": name,
        "repositoryUrl": f"https://github.com/example/{name}",
        "pipelineTemplate": "container-ci-cd-v1",
        "runtime": "docker",
    })
    assert created.status_code == 201, created.text
    application_id = created.json()["id"]
    run = client.post(f"/applications/{application_id}/pipeline-runs", json={
        "commitSha": "abc1234", "branch": "main", "environment": environment, "parameters": {},
    })
    assert run.status_code == 202, run.text
    return UUID(application_id), UUID(run.json()["id"])


def ci_token(application_id: UUID, run_id: UUID, scopes=None, **overrides):
    return workload_identity.mint(
        workload=overrides.pop("workload", Workload.JENKINS),
        application_id=application_id,
        pipeline_run_id=run_id,
        scopes=scopes or {Scope.CI_RESULT, Scope.CI_EVIDENCE},
        **overrides,
    )


# ------------------------------------------------------------- resource binding


def test_a_token_for_one_run_cannot_report_another_runs_result():
    application_a, run_a = start_run("cb-app-a")
    _, run_b = start_run("cb-app-b")
    token = ci_token(application_a, run_a)

    refused = client.post(
        f"/pipeline-runs/{run_b}/ci-result",
        json={"status": "running"},
        headers=bearer(token),
    )

    assert refused.status_code == 403
    assert refused.json()["code"] == "RESOURCE_MISMATCH"
    # And run B is untouched.
    assert client.get(f"/pipeline-runs/{run_b}").json()["status"] == "queued"


def test_the_same_token_works_for_the_run_it_names():
    application_id, run_id = start_run()
    token = ci_token(application_id, run_id)

    accepted = client.post(
        f"/pipeline-runs/{run_id}/ci-result", json={"status": "running"}, headers=bearer(token)
    )

    assert accepted.status_code == 202
    assert client.get(f"/pipeline-runs/{run_id}").json()["status"] == "running"


# --------------------------------------------------------------- workload kinds


def test_a_jenkins_token_cannot_report_a_deployment_result():
    application_id, run_id = start_run()
    ci = ci_token(application_id, run_id)
    client.post(f"/pipeline-runs/{run_id}/ci-result", json={"status": "running"}, headers=bearer(ci))
    client.post(f"/pipeline-runs/{run_id}/security-evidence", headers=bearer(ci), json={
        "artifactDigest": DIGEST,
        "sbom": {"generatedBy": "syft", "location": "s3://evidence/sbom.json"},
        "vulnerabilityScan": {"scanner": "trivy", "status": "passed", "critical": 0, "high": 0},
        "signature": {"provider": "cosign", "verified": True},
    })
    result = client.post(
        f"/pipeline-runs/{run_id}/ci-result",
        json={"status": "succeeded", "artifactDigest": DIGEST},
        headers=bearer(ci),
    )
    deployment_id = result.json()["deployment"]["id"]

    # A Jenkins token cannot even be minted with the deployment scope, so the closest a
    # controller can get is presenting its CI token at the deployment endpoint.
    refused = client.post(
        f"/deployments/{deployment_id}/result",
        json={"status": "healthy"},
        headers=bearer(ci),
    )

    assert refused.status_code == 403
    assert refused.json()["code"] in {"WORKLOAD_NOT_PERMITTED", "RESOURCE_MISMATCH"}
    assert client.get(f"/deployments/{deployment_id}").json()["status"] != "healthy"


def deploy_and_report(name: str = "cb-app"):
    """Drive a run to a healthy deployment through real callbacks, returning the token."""

    application_id, run_id = start_run(name)
    ci = ci_token(application_id, run_id)
    client.post(f"/pipeline-runs/{run_id}/ci-result", json={"status": "running"}, headers=bearer(ci))
    client.post(f"/pipeline-runs/{run_id}/security-evidence", headers=bearer(ci), json={
        "artifactDigest": DIGEST,
        "sbom": {"generatedBy": "syft", "location": "s3://evidence/sbom.json"},
        "vulnerabilityScan": {"scanner": "trivy", "status": "passed", "critical": 0, "high": 0},
        "signature": {"provider": "cosign", "verified": True},
    })
    deployment_id = client.post(
        f"/pipeline-runs/{run_id}/ci-result",
        json={"status": "succeeded", "artifactDigest": DIGEST},
        headers=bearer(ci),
    ).json()["deployment"]["id"]

    worker = workload_identity.mint(
        workload=Workload.TEMPORAL, application_id=application_id,
        deployment_id=UUID(deployment_id), scopes={Scope.DEPLOYMENT_RESULT},
    )
    accepted = client.post(
        f"/deployments/{deployment_id}/result", json={"status": "healthy"}, headers=bearer(worker)
    )

    assert accepted.status_code == 202
    assert client.get(f"/deployments/{deployment_id}").json()["status"] == "healthy"
    return application_id, deployment_id, worker


def test_the_temporal_workload_reports_the_deployment_it_was_issued_for():
    deploy_and_report()


# ------------------------------------------------------------ replay and expiry


def test_a_terminal_token_cannot_be_replayed():
    """A token scraped from a worker must not be able to overwrite a later result."""

    _, deployment_id, worker = deploy_and_report("cb-replay")

    replayed = client.post(
        f"/deployments/{deployment_id}/result", json={"status": "failed"}, headers=bearer(worker)
    )

    assert replayed.status_code == 401
    assert replayed.json()["code"] == "TOKEN_REPLAYED"
    assert client.get(f"/deployments/{deployment_id}").json()["status"] == "healthy"


def test_an_expired_token_is_refused():
    application_id, run_id = start_run()
    token = workload_identity.mint(
        workload=Workload.JENKINS, application_id=application_id, pipeline_run_id=run_id,
        scopes={Scope.CI_RESULT}, ttl_seconds=60,
        now=int(time.time()) - 3600,
    )

    refused = client.post(
        f"/pipeline-runs/{run_id}/ci-result", json={"status": "running"}, headers=bearer(token)
    )

    assert refused.status_code == 401
    assert refused.json()["code"] == "TOKEN_EXPIRED"


def test_a_token_for_another_audience_is_refused(monkeypatch):
    application_id, run_id = start_run()
    token = ci_token(application_id, run_id)
    monkeypatch.setenv("NETCI_WORKLOAD_TOKEN_AUDIENCE", "a-different-netci")

    refused = client.post(
        f"/pipeline-runs/{run_id}/ci-result", json={"status": "running"}, headers=bearer(token)
    )

    assert refused.status_code == 401
    assert refused.json()["code"] == "INVALID_AUDIENCE"


def test_a_token_without_the_scope_is_refused():
    application_id, run_id = start_run()
    # Evidence only: a real token, for the right run, lacking the result scope.
    token = ci_token(application_id, run_id, scopes={Scope.CI_EVIDENCE})

    refused = client.post(
        f"/pipeline-runs/{run_id}/ci-result", json={"status": "running"}, headers=bearer(token)
    )

    assert refused.status_code == 403
    assert refused.json()["code"] == "SCOPE_NOT_PERMITTED"


# ---------------------------------------------------------------- no leakage


def test_no_response_or_audit_record_contains_the_token_or_the_signing_key():
    application_id, run_id = start_run()
    token = ci_token(application_id, run_id)
    client.post(f"/pipeline-runs/{run_id}/ci-result", json={"status": "running"}, headers=bearer(token))

    logs = client.get(f"/pipeline-runs/{run_id}/logs").text
    audit = client.get("/audit-events").text
    health = client.get("/healthz").text

    for rendered in (logs, audit, health):
        assert token not in rendered
        assert "z" * 48 not in rendered
        assert "NETCI_WORKLOAD_TOKEN_KEYS" not in rendered


def test_the_minting_endpoint_returns_a_usable_token_and_stores_only_its_jti():
    application_id, run_id = start_run()

    issued = client.post(f"/pipeline-runs/{run_id}/callback-token", json={
        "workload": "jenkins", "scopes": [Scope.CI_RESULT], "ttlSeconds": 600,
    })

    assert issued.status_code == 201
    token = issued.json()["token"]
    used = client.post(
        f"/pipeline-runs/{run_id}/ci-result", json={"status": "running"}, headers=bearer(token)
    )
    assert used.status_code == 202
    # The token is returned once and is not readable back out of netCI.
    assert token not in client.get(f"/pipeline-runs/{run_id}").text


def test_the_minting_endpoint_refuses_a_scope_the_workload_may_not_hold():
    _, run_id = start_run()

    refused = client.post(f"/pipeline-runs/{run_id}/callback-token", json={
        "workload": "jenkins", "scopes": [Scope.DEPLOYMENT_RESULT],
    })

    assert refused.status_code == 422
    assert refused.json()["code"] == "SCOPE_NOT_PERMITTED"
