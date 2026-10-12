"""SLSA provenance: what an artifact was built from, checked twice (ADR-044).

A signature proves the key signed *something*. Provenance says the image came from the
commit netCI dispatched, of the module's repository. The API binds it to the run when
evidence is published; the worker re-reads the signed attestation from the registry just
before deploying and compares it again.
"""

from __future__ import annotations

import base64
import importlib.util
import json
from pathlib import Path

import pytest

from app.adapters.signature_verifier import (
    ArtifactIdentity,
    CosignSignatureVerifier,
    NullSignatureVerifier,
    SignatureVerificationError,
    check_provenance_statements,
)
from app.policy.rules import PolicyViolation, evaluate_artifact_evidence, public_repository_url
from app.workflows.activities import DeliveryActivities, FileEvidenceStore
from app.workflows.provision_and_deploy import DeliveryInput

DIGEST = "sha256:" + "a" * 64
COMMIT = "c" * 40
REPO = "http://git.example/acme/orders.git"
SLSA = "https://slsa.dev/provenance/v1"


def evidence(provenance=None, artifact_ref=f"registry.local/orders@{DIGEST}"):
    body = {
        "artifactDigest": DIGEST,
        "artifactRef": artifact_ref,
        "sbom": {"generatedBy": "syft", "location": "s3://e/sbom.json"},
        "vulnerabilityScan": {"scanner": "trivy", "status": "passed", "critical": 0, "high": 0},
        "signature": {"provider": "cosign", "verified": True},
    }
    if provenance is not None:
        body["provenance"] = provenance
    return body


def provenance(**over):
    return {"predicateType": SLSA, "verified": True, "repository": REPO, "commit": COMMIT, **over}


def decide(ev, *, required=False, source=(REPO, COMMIT)):
    return evaluate_artifact_evidence(ev, expected_digest=DIGEST, require_evidence=True,
                                      expected_source=source, require_provenance=required, exceptions=())


# ------------------------------------------------------------------ policy


def test_provenance_naming_the_dispatched_commit_passes():
    decision = decide(evidence(provenance()))
    assert decision.allowed and decision.checks["provenance"] == "pass"


def test_provenance_from_another_commit_is_refused():
    decision = decide(evidence(provenance(commit="d" * 40)))
    assert not decision.allowed and "netCI dispatched" in decision.reason


def test_provenance_from_another_repository_is_refused():
    decision = decide(evidence(provenance(repository="http://git.example/evil/orders.git")))
    assert not decision.allowed and "another repository" in decision.reason


def test_credentials_in_the_repository_url_do_not_make_it_another_repository():
    decision = decide(evidence(provenance()), source=("http://bot:tok3n@git.example/acme/orders.git", COMMIT))
    assert decision.allowed
    assert public_repository_url("https://u:p@git.example:8443/a.git/") == "https://git.example:8443/a.git"


def test_unverified_provenance_is_refused_even_when_not_required():
    assert not decide(evidence(provenance(verified=False))).allowed


def test_required_provenance_refuses_an_image_without_it():
    decision = decide(evidence(), required=True)
    assert not decision.allowed and "no verified SLSA provenance" in decision.reason


def test_required_provenance_does_not_apply_to_a_signed_binary():
    decision = decide(evidence(artifact_ref="file:///srv/artifacts/app.bin"), required=True)
    assert decision.allowed and decision.checks["provenance"] == "not_applicable"


def test_without_the_requirement_missing_provenance_is_recorded_as_such():
    assert decide(evidence()).checks["provenance"] == "not_required"


# ------------------------------------------------------------------ attestation output


def _envelope(*, digest=DIGEST, commit=COMMIT, repository=REPO, predicate_type=SLSA):
    statement = {
        "_type": "https://in-toto.io/Statement/v1",
        "subject": [{"name": "registry.local/orders", "digest": {"sha256": digest.removeprefix("sha256:")}}],
        "predicateType": predicate_type,
        "predicate": {"buildDefinition": {
            "externalParameters": {"repository": repository, "commit": commit},
            "resolvedDependencies": [{"uri": f"git+{repository}", "digest": {"gitCommit": commit}}],
        }},
    }
    payload = base64.b64encode(json.dumps(statement).encode()).decode()
    return json.dumps({"payloadType": "application/vnd.in-toto+json", "payload": payload, "signatures": [{}]})


def test_a_statement_for_this_digest_commit_and_repository_is_accepted():
    output = "Verification for registry.local/orders --\n" + _envelope()
    assert "built from" in check_provenance_statements(output, digest=DIGEST, commit=COMMIT, repository=REPO)


def test_a_statement_about_another_commit_is_named_in_the_refusal():
    with pytest.raises(SignatureVerificationError, match="netCI dispatched"):
        check_provenance_statements(_envelope(commit="e" * 40), digest=DIGEST, commit=COMMIT, repository=REPO)


@pytest.mark.parametrize("envelope", [
    _envelope(digest="sha256:" + "f" * 64),
    _envelope(predicate_type="https://slsa.dev/provenance/v0.2"),
    "not json at all",
])
def test_nothing_that_names_this_digest_as_slsa_v1_is_no_provenance(envelope):
    with pytest.raises(SignatureVerificationError, match="no verified SLSA v1"):
        check_provenance_statements(envelope, digest=DIGEST, commit=COMMIT, repository=REPO)


@pytest.mark.asyncio
async def test_the_cosign_verifier_runs_verify_attestation_against_the_pinned_digest(monkeypatch):
    verifier = CosignSignatureVerifier(key="/keys/cosign.pub")
    seen = {}

    async def fake_run(command):
        seen["command"] = command
        return 0, _envelope()

    monkeypatch.setattr(verifier, "_run", fake_run)
    outcome = await verifier.verify_provenance(
        ArtifactIdentity(digest=DIGEST, reference="registry.local/orders:latest"), commit=COMMIT, repository=REPO)
    assert "built from" in outcome
    assert seen["command"][:6] == ["cosign", "verify-attestation", "--key", "/keys/cosign.pub", "--type", "slsaprovenance1"]
    assert seen["command"][-1] == f"registry.local/orders@{DIGEST}"


@pytest.mark.asyncio
async def test_the_null_verifier_refuses_rather_than_pretending():
    with pytest.raises(SignatureVerificationError, match="cannot verify"):
        await NullSignatureVerifier().verify_provenance(
            ArtifactIdentity(digest=DIGEST, reference="r@x"), commit=COMMIT, repository=REPO)


# ------------------------------------------------------------------ the worker


class _Runtime:
    async def deploy(self, delivery):
        return "d"

    async def health_check(self, delivery):
        return True

    async def rollback(self, delivery):
        return None


class _Verifier:
    mode = "fake"

    def __init__(self, provenance_error=None):
        self.provenance_error = provenance_error
        self.provenance_calls = []

    async def verify(self, artifact):
        return "signature ok"

    async def verify_provenance(self, artifact, *, commit, repository):
        self.provenance_calls.append((artifact.digest, commit, repository))
        if self.provenance_error:
            raise SignatureVerificationError(self.provenance_error)
        return "provenance ok"


def _delivery():
    return DeliveryInput(application_id="app-1", pipeline_run_id="run-1", runtime="docker", environment="dev",
                         artifact_digest=DIGEST, commit_sha=COMMIT, source_repository=REPO)


def _store(tmp_path: Path, **over) -> FileEvidenceStore:
    body = {**evidence(provenance()), "applicationId": "app-1", "decision": "allow", **over}
    (tmp_path / "run-1.json").write_text(json.dumps(body), encoding="utf-8")
    return FileEvidenceStore(tmp_path)


@pytest.mark.asyncio
async def test_the_worker_checks_provenance_against_the_deployment_commit_when_required(tmp_path, monkeypatch):
    monkeypatch.setenv("NETCI_REQUIRE_PROVENANCE", "true")
    verifier = _Verifier()
    await DeliveryActivities(_store(tmp_path), _Runtime(), verifier).validate_artifact(_delivery())
    assert verifier.provenance_calls == [(DIGEST, COMMIT, REPO)]


@pytest.mark.asyncio
async def test_a_provenance_mismatch_at_the_worker_stops_the_deployment(tmp_path, monkeypatch):
    monkeypatch.setenv("NETCI_REQUIRE_PROVENANCE", "true")
    verifier = _Verifier(provenance_error="says evil@deadbeef")
    with pytest.raises(PolicyViolation, match="provenance verification failed"):
        await DeliveryActivities(_store(tmp_path), _Runtime(), verifier).validate_artifact(_delivery())


@pytest.mark.asyncio
async def test_the_worker_does_not_ask_for_provenance_when_it_is_not_required(tmp_path, monkeypatch):
    monkeypatch.setenv("NETCI_REQUIRE_PROVENANCE", "false")
    verifier = _Verifier()
    await DeliveryActivities(_store(tmp_path), _Runtime(), verifier).validate_artifact(_delivery())
    assert verifier.provenance_calls == []


# ------------------------------------------------------------------ what CI writes


def _callback_module():
    path = Path(__file__).resolve().parents[2] / "scripts" / "netci_callback.py"
    spec = importlib.util.spec_from_file_location("netci_callback_under_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_ci_provenance_records_the_dispatch_and_never_a_credential(monkeypatch, tmp_path):
    callback = _callback_module()
    monkeypatch.setenv("GIT_URL", "https://ci-bot:s3cret@git.example/acme/orders.git")
    monkeypatch.setenv("COMMIT_SHA", COMMIT)
    monkeypatch.setenv("WORKSPACE", str(tmp_path))
    monkeypatch.setenv("NETCI_APP_DIR", str(tmp_path / "services" / "orders"))
    monkeypatch.setenv("NETCI_IMAGE_NAME", "orders")
    monkeypatch.setenv("JENKINS_URL", "http://jenkins-a:8080/")
    monkeypatch.setenv("BUILD_URL", "http://jenkins-a:8080/job/x/3/")

    predicate = callback.provenance_predicate()
    text = json.dumps(predicate)
    assert "s3cret" not in text and "ci-bot" not in text
    external = predicate["buildDefinition"]["externalParameters"]
    assert external["repository"] == "https://git.example/acme/orders.git"
    assert external["commit"] == COMMIT and external["appDir"] == "services/orders"
    assert predicate["buildDefinition"]["resolvedDependencies"][0]["digest"] == {"gitCommit": COMMIT}
    assert predicate["runDetails"]["builder"]["id"] == "http://jenkins-a:8080#netci-shared-library"


def test_ci_provenance_refuses_a_build_netci_did_not_dispatch(monkeypatch):
    callback = _callback_module()
    monkeypatch.delenv("GIT_URL", raising=False)
    monkeypatch.delenv("COMMIT_SHA", raising=False)
    with pytest.raises(SystemExit):
        callback.provenance_predicate()


# ------------------------------------------------------------------ the API binds it to the run


def test_publishing_provenance_for_another_commit_is_denied_and_the_denial_binds(monkeypatch):
    from fastapi.testclient import TestClient

    import app.main as main_mod

    main_mod.platform.reset()
    client = TestClient(main_mod.app)
    machine = {"Authorization": "Bearer netci-local-pipeline-key"}
    application = client.post("/applications", json={
        "name": "prov-app", "repositoryUrl": REPO, "pipelineTemplate": "container-ci-cd-v1", "runtime": "docker",
    }).json()
    run = client.post(f"/applications/{application['id']}/pipeline-runs",
                      json={"commitSha": COMMIT, "environment": "dev"}).json()
    client.post(f"/pipeline-runs/{run['id']}/ci-result", headers=machine, json={"status": "running"})

    body = {**evidence(), "provenance": provenance(commit="d" * 40)}
    body["sbom"]["format"] = "cyclonedx-json"
    published = client.post(f"/pipeline-runs/{run['id']}/security-evidence", headers=machine, json=body)
    assert published.status_code == 202, published.text
    assert published.json()["decision"] == "deny"
    assert "netCI dispatched" in published.json()["reason"]

    completed = client.post(f"/pipeline-runs/{run['id']}/ci-result", headers=machine,
                            json={"status": "succeeded", "artifactDigest": DIGEST})
    assert completed.status_code == 422
    assert completed.json()["code"] == "ARTIFACT_POLICY_DENIED"
