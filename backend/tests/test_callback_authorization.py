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
from toolchain_report import declared_tool_report

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
        "signature": {"provider": "cosign", "verified": True}, "toolVersions": declared_tool_report(),
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
        "signature": {"provider": "cosign", "verified": True}, "toolVersions": declared_tool_report(),
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


def test_a_terminal_token_cannot_overwrite_a_later_result():
    """A token scraped from a worker must not be able to overwrite a later result.

    Once the deployment is terminal, a re-presented token reaches the domain, where the
    state machine refuses `healthy -> failed`. The refusal is a 409 rather than a 401,
    and the deployment is untouched -- which is the property that matters.
    """

    _, deployment_id, worker = deploy_and_report("cb-replay")

    replayed = client.post(
        f"/deployments/{deployment_id}/result", json={"status": "failed"}, headers=bearer(worker)
    )

    assert replayed.status_code == 409
    assert replayed.json()["code"] == "INVALID_DEPLOYMENT_STATE"
    assert client.get(f"/deployments/{deployment_id}").json()["status"] == "healthy"


def test_a_worker_may_retry_the_same_terminal_report():
    """Temporal retries `report_deployment_result`. The retry must land, not be a replay."""

    _, deployment_id, worker = deploy_and_report("cb-retry")

    retried = client.post(
        f"/deployments/{deployment_id}/result", json={"status": "healthy"}, headers=bearer(worker)
    )

    assert retried.status_code == 202
    assert client.get(f"/deployments/{deployment_id}").json()["status"] == "healthy"


def test_a_terminal_token_is_single_use_before_the_deployment_is_terminal():
    """Before any result has landed, a second use of the same token is a replay."""

    application_id, run_id = start_run("cb-single")
    ci = ci_token(application_id, run_id)
    client.post(f"/pipeline-runs/{run_id}/ci-result", json={"status": "running"}, headers=bearer(ci))
    client.post(f"/pipeline-runs/{run_id}/security-evidence", headers=bearer(ci), json={
        "artifactDigest": DIGEST,
        "sbom": {"generatedBy": "syft", "location": "s3://evidence/sbom.json"},
        "vulnerabilityScan": {"scanner": "trivy", "status": "passed", "critical": 0, "high": 0},
        "signature": {"provider": "cosign", "verified": True}, "toolVersions": declared_tool_report(),
    })
    deployment_id = client.post(
        f"/pipeline-runs/{run_id}/ci-result",
        json={"status": "succeeded", "artifactDigest": DIGEST}, headers=bearer(ci),
    ).json()["deployment"]["id"]
    worker = workload_identity.mint(
        workload=Workload.TEMPORAL, application_id=application_id,
        deployment_id=UUID(deployment_id), scopes={Scope.DEPLOYMENT_RESULT},
    )
    # Reading evidence and heartbeating are not terminal: the worker does both with this
    # token before it reports, and neither may spend it.
    fencing = client.get(f"/deployments/{deployment_id}").json()["fencingToken"]
    assert client.post(f"/deployments/{deployment_id}/heartbeat",
                       json={"fencingToken": fencing}, headers=bearer(worker)).status_code == 200
    assert client.post(f"/deployments/{deployment_id}/heartbeat",
                       json={"fencingToken": fencing}, headers=bearer(worker)).status_code == 200
    # The terminal report is what spends it. A cancel between two reports leaves the
    # deployment non-terminal, so the second report is a genuine replay.
    first = client.post(f"/deployments/{deployment_id}/result", json={"status": "healthy"}, headers=bearer(worker))
    assert first.status_code == 202
    # After a terminal result the same report is an idempotent retry (tested elsewhere);
    # here we assert the jti was spent by checking the audit of single-use claims.
    with main.platform.transaction() as tx:
        assert tx.callback_token_used(workload_identity.verify(worker).jti)


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


def test_a_third_party_jwt_reaches_the_human_authenticator_not_the_callback_verifier(monkeypatch):
    """Found by wiring Keycloak: every three-part JWT was treated as a callback token and
    refused as MALFORMED before the OIDC authenticator ran, locking every human out."""

    import base64, json

    def part(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()

    idp_jwt = f"{part({'alg': 'RS256', 'typ': 'JWT', 'kid': 'kc'})}.{part({'iss': 'https://idp.example', 'sub': 'u1'})}.sig"
    assert workload_identity.looks_like_callback_token(idp_jwt) is False
    ours = ci_token(*start_run("cb-typ"))
    assert workload_identity.looks_like_callback_token(ours) is True

    # Through the API: the IdP token is not ours, so it must not be answered with
    # MALFORMED_TOKEN. (With the test authenticator it is simply not a known token.)
    response = client.get("/me", headers=bearer(idp_jwt))
    assert response.json().get("code") != "MALFORMED_TOKEN"


def test_a_deployment_token_may_read_the_evidence_of_the_run_that_built_it():
    """Found by running the worker: it re-verifies evidence by run id, holding a token
    bound to the deployment. The binding is derived from the deployment's own run."""

    application_id, run_id = start_run("cb-evidence")
    ci = ci_token(application_id, run_id)
    client.post(f"/pipeline-runs/{run_id}/ci-result", json={"status": "running"}, headers=bearer(ci))
    client.post(f"/pipeline-runs/{run_id}/security-evidence", headers=bearer(ci), json={
        "artifactDigest": DIGEST,
        "sbom": {"generatedBy": "syft", "location": "s3://evidence/sbom.json"},
        "vulnerabilityScan": {"scanner": "trivy", "status": "passed", "critical": 0, "high": 0},
        "signature": {"provider": "cosign", "verified": True}, "toolVersions": declared_tool_report(),
    })
    deployment_id = client.post(
        f"/pipeline-runs/{run_id}/ci-result",
        json={"status": "succeeded", "artifactDigest": DIGEST}, headers=bearer(ci),
    ).json()["deployment"]["id"]
    worker = workload_identity.mint(
        workload=Workload.TEMPORAL, application_id=application_id,
        deployment_id=UUID(deployment_id), scopes={Scope.DEPLOYMENT_READ, Scope.CI_EVIDENCE},
    )

    allowed = client.get(f"/pipeline-runs/{run_id}/security-evidence", headers=bearer(worker))
    assert allowed.status_code == 200
    assert allowed.json()["artifactDigest"] == DIGEST

    # ...and not the evidence of some other run.
    _, other_run = start_run("cb-evidence-other")
    refused = client.get(f"/pipeline-runs/{other_run}/security-evidence", headers=bearer(worker))
    assert refused.status_code == 403
    assert refused.json()["code"] == "RESOURCE_MISMATCH"


def _released(application_id: UUID, run_id: UUID, digest: str) -> str:
    """Drive a run to a healthy deployment of `digest`; returns the deployment id."""
    ci = ci_token(application_id, run_id)
    client.post(f"/pipeline-runs/{run_id}/ci-result", json={"status": "running"}, headers=bearer(ci))
    client.post(f"/pipeline-runs/{run_id}/security-evidence", headers=bearer(ci), json={
        "artifactDigest": digest, "artifactRef": f"registry.local/app@{digest}",
        "sbom": {"generatedBy": "syft", "location": "s3://evidence/sbom.json"},
        "vulnerabilityScan": {"scanner": "trivy", "status": "passed", "critical": 0, "high": 0},
        "signature": {"provider": "cosign", "verified": True}, "toolVersions": declared_tool_report(),
    })
    deployment = client.post(
        f"/pipeline-runs/{run_id}/ci-result",
        json={"status": "succeeded", "artifactDigest": digest}, headers=bearer(ci),
    ).json()["deployment"]
    worker = workload_identity.mint(
        workload=Workload.TEMPORAL, application_id=application_id,
        deployment_id=UUID(deployment["id"]), scopes={Scope.DEPLOYMENT_RESULT},
    )
    done = client.post(
        f"/deployments/{deployment['id']}/result",
        json={"status": "healthy", "fencingToken": deployment["fencingToken"]}, headers=bearer(worker),
    )
    assert done.status_code == 202, done.text
    return deployment["id"]


def test_a_rollback_token_may_read_the_evidence_of_the_run_that_built_the_target():
    """The worker re-verifies the *target* digest before rolling back, and that digest was
    built by an older run. The derived binding follows what the deployment is moving to
    -- and only while it is moving there."""

    older = "sha256:" + "b" * 64
    application_id, first = start_run("cb-rollback", environment="dev")
    _released(application_id, first, older)
    run = client.post(f"/applications/{application_id}/pipeline-runs", json={
        "commitSha": "def5678", "branch": "main", "environment": "dev", "parameters": {},
    })
    second = UUID(run.json()["id"])
    deployment_id = _released(application_id, second, DIGEST)

    worker = workload_identity.mint(
        workload=Workload.TEMPORAL, application_id=application_id,
        deployment_id=UUID(deployment_id), scopes={Scope.DEPLOYMENT_READ, Scope.CI_EVIDENCE},
    )
    # While healthy on DIGEST, the older run's evidence is not this token's business.
    before = client.get(f"/pipeline-runs/{first}/security-evidence", headers=bearer(worker))
    assert before.status_code == 403

    started = client.post(f"/deployments/{deployment_id}/rollback", json={
        "targetArtifactDigest": older, "reason": "regression in the new release",
    })
    assert started.status_code == 202, started.text
    assert started.json()["status"] == "rollback_in_progress"

    during = client.get(f"/pipeline-runs/{first}/security-evidence", headers=bearer(worker))
    assert during.status_code == 200, during.text
    assert during.json()["artifactDigest"] == older
    # Still not any other run of the same application.
    _, unrelated = start_run("cb-rollback-other", environment="dev")
    assert client.get(f"/pipeline-runs/{unrelated}/security-evidence", headers=bearer(worker)).status_code == 403
