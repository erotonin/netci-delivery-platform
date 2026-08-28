"""Supply-chain policy tests: what netCI refuses to deploy, and why.

The deny cases are the point. A pipeline that reports success is not evidence; the
artifact must carry a Syft SBOM, a clean Trivy scan and a verified Cosign signature
for the digest actually being deployed.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.delivery import DeliveryError
from app.domain.models import DeploymentStatus, Environment, PipelineStatus
from app.main import app
from app.policy.rules import evaluate_artifact_evidence

client = TestClient(app)
MACHINE_HEADERS = {"Authorization": "Bearer netci-local-pipeline-key"}
DIGEST = "sha256:" + "c" * 64
OTHER_DIGEST = "sha256:" + "d" * 64


@pytest.fixture(autouse=True)
def reset_state():
    main.platform.reset()
    main.portal.reset()


def clean_evidence(digest: str = DIGEST) -> dict[str, object]:
    return {
        "artifactDigest": digest,
        "artifactRef": f"localhost:5000/hello-container@{digest}",
        "sbom": {"generatedBy": "syft", "location": "s3://netci-evidence/sbom.json", "format": "cyclonedx-json"},
        "vulnerabilityScan": {"scanner": "trivy", "status": "passed", "critical": 0, "high": 0, "medium": 2},
        "signature": {"provider": "cosign", "verified": True, "certificateIdentity": "netci-local"},
    }


def create_application(name: str = "secure-app") -> str:
    response = client.post(
        "/applications",
        json={
            "name": name,
            "repositoryUrl": f"https://github.com/example/{name}",
            "pipelineTemplate": "container-ci-cd-v1",
            "runtime": "docker",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def start_run(application_id: str, environment: str = "staging") -> str:
    response = client.post(
        f"/applications/{application_id}/pipeline-runs",
        json={"commitSha": "abc1234", "environment": environment},
    )
    assert response.status_code == 202, response.text
    run_id = response.json()["id"]
    assert client.post(
        f"/pipeline-runs/{run_id}/ci-result", json={"status": "running"}, headers=MACHINE_HEADERS
    ).status_code == 202
    return run_id


# ------------------------------------------------------------------- unit level


@pytest.mark.parametrize(
    "mutation,expected",
    [
        ({"sbom": {"generatedBy": "handwritten", "location": "s3://x"}}, "Syft"),
        ({"sbom": {"generatedBy": "syft", "location": ""}}, "Syft"),
        ({"vulnerabilityScan": {"scanner": "grype", "status": "passed"}}, "Trivy"),
        ({"vulnerabilityScan": {"scanner": "trivy", "status": "passed", "critical": 1, "high": 0}}, "critical"),
        ({"vulnerabilityScan": {"scanner": "trivy", "status": "passed", "critical": 0, "high": 4}}, "high"),
        ({"vulnerabilityScan": {"scanner": "trivy", "status": "failed", "critical": 0, "high": 0}}, "did not pass"),
        ({"signature": {"provider": "cosign", "verified": False}}, "signature"),
        ({"signature": {"provider": "notary", "verified": True}}, "signature"),
        ({"artifactDigest": OTHER_DIGEST}, "does not describe the artifact"),
    ],
)
def test_each_missing_or_failing_control_denies_the_artifact(mutation, expected):
    decision = evaluate_artifact_evidence(
        {**clean_evidence(), **mutation}, expected_digest=DIGEST, require_evidence=True
    )

    assert decision.allowed is False
    assert expected in decision.reason


def test_complete_evidence_for_the_right_digest_is_allowed():
    decision = evaluate_artifact_evidence(clean_evidence(), expected_digest=DIGEST, require_evidence=True)

    assert decision.allowed is True
    assert decision.checks == {
        "digest": "pass",
        "evidenceDigest": "pass",
        "sbom": "pass",
        "vulnerabilityScan": "pass",
        "signature": "pass",
    }


def test_a_tag_instead_of_a_digest_is_never_deployable():
    decision = evaluate_artifact_evidence(None, expected_digest="latest", require_evidence=False)

    assert decision.allowed is False
    assert "immutable sha256" in decision.reason


# -------------------------------------------------------------------- API level


def test_publishing_clean_evidence_returns_an_allow_decision():
    application_id = create_application()
    run_id = start_run(application_id)

    response = client.post(
        f"/pipeline-runs/{run_id}/security-evidence", json=clean_evidence(), headers=MACHINE_HEADERS
    )

    assert response.status_code == 202, response.text
    assert response.json()["decision"] == "allow"
    stored = client.get(f"/pipeline-runs/{run_id}/security-evidence")
    assert stored.status_code == 200
    assert stored.json()["artifactDigest"] == DIGEST


def test_publishing_evidence_requires_the_pipeline_api_key():
    application_id = create_application()
    run_id = start_run(application_id)

    response = client.post(f"/pipeline-runs/{run_id}/security-evidence", json=clean_evidence())

    assert response.status_code == 401
    assert response.json()["code"] == "PIPELINE_UNAUTHORIZED"


def test_a_vulnerable_artifact_is_refused_and_the_run_fails():
    application_id = create_application()
    run_id = start_run(application_id)
    vulnerable = clean_evidence()
    vulnerable["vulnerabilityScan"] = {"scanner": "trivy", "status": "failed", "critical": 2, "high": 5}
    published = client.post(
        f"/pipeline-runs/{run_id}/security-evidence", json=vulnerable, headers=MACHINE_HEADERS
    )
    assert published.json()["decision"] == "deny"

    response = client.post(
        f"/pipeline-runs/{run_id}/ci-result",
        json={"status": "succeeded", "artifactDigest": DIGEST},
        headers=MACHINE_HEADERS,
    )

    assert response.status_code == 422
    assert response.json()["code"] == "ARTIFACT_POLICY_DENIED"
    assert client.get(f"/pipeline-runs/{run_id}").json()["status"] == "failed"
    # No deployment may exist for an artifact that failed policy.
    assert main.platform.list_deployments() == ()


def test_evidence_for_a_different_digest_does_not_authorise_this_artifact():
    application_id = create_application()
    run_id = start_run(application_id)
    client.post(
        f"/pipeline-runs/{run_id}/security-evidence", json=clean_evidence(OTHER_DIGEST), headers=MACHINE_HEADERS
    )

    response = client.post(
        f"/pipeline-runs/{run_id}/ci-result",
        json={"status": "succeeded", "artifactDigest": DIGEST},
        headers=MACHINE_HEADERS,
    )

    assert response.status_code == 422
    assert response.json()["code"] == "ARTIFACT_POLICY_DENIED"


def test_clean_evidence_lets_the_deployment_proceed():
    application_id = create_application()
    run_id = start_run(application_id)
    client.post(f"/pipeline-runs/{run_id}/security-evidence", json=clean_evidence(), headers=MACHINE_HEADERS)

    response = client.post(
        f"/pipeline-runs/{run_id}/ci-result",
        json={"status": "succeeded", "artifactDigest": DIGEST},
        headers=MACHINE_HEADERS,
    )

    assert response.status_code == 202, response.text
    assert response.json()["deployment"]["status"] == DeploymentStatus.DEPLOYING.value


def test_when_evidence_is_mandatory_an_unevidenced_artifact_is_refused(monkeypatch):
    monkeypatch.setenv("NETCI_REQUIRE_SECURITY_EVIDENCE", "true")
    application_id = create_application()
    run_id = start_run(application_id)

    response = client.post(
        f"/pipeline-runs/{run_id}/ci-result",
        json={"status": "succeeded", "artifactDigest": DIGEST},
        headers=MACHINE_HEADERS,
    )

    assert response.status_code == 422
    assert response.json()["code"] == "ARTIFACT_POLICY_DENIED"
    assert "no security evidence" in response.json()["message"]


def test_the_policy_decision_is_written_to_the_pipeline_log_for_audit():
    application_id = create_application()
    run_id = start_run(application_id)
    client.post(f"/pipeline-runs/{run_id}/security-evidence", json=clean_evidence(), headers=MACHINE_HEADERS)
    client.post(
        f"/pipeline-runs/{run_id}/ci-result",
        json={"status": "succeeded", "artifactDigest": DIGEST},
        headers=MACHINE_HEADERS,
    )

    lines = client.get(f"/pipeline-runs/{run_id}/logs").json()["lines"]

    assert any(line.startswith("security-evidence decision=allow") for line in lines)
    assert any(line.startswith("policy=allow") for line in lines)


def test_evidence_must_reference_an_immutable_digest():
    application_id = create_application()
    run_id = start_run(application_id)
    bad = clean_evidence()
    bad["artifactDigest"] = "latest"

    response = client.post(f"/pipeline-runs/{run_id}/security-evidence", json=bad, headers=MACHINE_HEADERS)

    assert response.status_code == 422


def test_evidence_for_an_unknown_run_is_rejected():
    with pytest.raises(DeliveryError):
        main.platform.record_security_evidence(
            __import__("uuid").uuid4(), clean_evidence()
        )
