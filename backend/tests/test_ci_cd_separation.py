"""CI and CD as separate decisions, through the API (ADR-043).

Before this, every run built *and* deployed and every webhook deployed to the default
environment: a push to a feature branch replaced what `dev` served, and a fork's pull
request ran the Sign stage with the signing key bound. These tests drive the webhook,
the CI callback and the promotion route the way Jenkins and a person would.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

import app.main as main_mod
from app.adapters.scm import MockScmProvider, get_scm_provider, set_scm_provider
from app.domain.models import ScmProviderType

client = TestClient(main_mod.app)
MACHINE = {"Authorization": "Bearer netci-local-pipeline-key"}
SECRET = "webhook-secret-for-tests"


@pytest.fixture(autouse=True)
def fresh_platform():
    main_mod.platform.reset()
    original = get_scm_provider(ScmProviderType.GITHUB)
    set_scm_provider(ScmProviderType.GITHUB, MockScmProvider(ScmProviderType.GITHUB))
    yield
    set_scm_provider(ScmProviderType.GITHUB, original)


def _module(name="orders-api", *, delivery=None, build_inputs=None, environments=("dev", "staging", "prod")):
    system = f"sys-{uuid4().hex[:6]}"
    assert client.post("/systems", json={"id": system, "unit": "Orders", "description": "orders"}).status_code == 201
    pipeline_config = None
    if delivery is not None or build_inputs is not None:
        pipeline_config = {"runner": "jenkins", "strategy": "Trunk-based",
                           "pipelines": {"ci": {"branch": "main", "stages": ["build"]}}}
        if delivery is not None:
            pipeline_config["delivery"] = delivery
        if build_inputs is not None:
            pipeline_config["buildInputs"] = build_inputs
    body = {
        "name": name, "displayName": name, "repositoryUrl": f"https://github.com/acme/{name}",
        "pipelineTemplate": "container-ci-cd-v1", "runtime": "docker", "moduleType": "Backend",
        "description": "test module",
        "deploymentEnvironments": [
            {"displayName": env, "environment": env, "runtime": "docker", "servers": [f"{env}-host"]}
            for env in environments
        ],
    }
    if pipeline_config is not None:
        body["pipelineConfig"] = pipeline_config
    created = client.post(f"/systems/{system}/modules", json=body)
    assert created.status_code == 201, created.text
    module = created.json()
    repo = f"acme/{name}-{uuid4().hex[:4]}"
    scm = client.post(f"/applications/{module['applicationId']}/scm",
                      json={"provider": "github", "repositoryIdentity": repo, "secretToken": SECRET})
    assert scm.status_code == 201, scm.text
    return module, repo


def _hook(event: str, payload: dict):
    raw = json.dumps(payload).encode()
    signature = hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()
    return client.post("/webhooks/scm/github", content=raw, headers={
        "x-github-delivery": uuid4().hex, "x-github-event": event,
        "x-hub-signature-256": f"sha256={signature}", "content-type": "application/json",
    })


def _push(repo, ref, sha=None):
    return _hook("push", {"repository": {"full_name": repo}, "ref": ref,
                          "after": sha or uuid4().hex + uuid4().hex[:8], "sender": {"login": "dev1"}})


def _pull_request(repo, *, fork=False, base="main", number=7):
    head_repo = "outsider/fork" if fork else repo
    return _hook("pull_request", {
        "action": "opened", "repository": {"full_name": repo}, "sender": {"login": "contributor"},
        "pull_request": {"number": number,
                         "head": {"sha": uuid4().hex + uuid4().hex[:8], "ref": "feature/x", "repo": {"full_name": head_repo}},
                         "base": {"ref": base, "repo": {"full_name": repo}}},
    })


def _succeed(run_id, digest=None, *, evidence=True):
    """What the library does: report running, publish evidence, report success."""

    client.post(f"/pipeline-runs/{run_id}/ci-result", headers=MACHINE, json={"status": "running"})
    body = {"status": "succeeded"}
    if digest is not False:
        body["artifactDigest"] = digest or f"sha256:{uuid4().hex}{uuid4().hex}"
        if evidence:
            published = client.post(f"/pipeline-runs/{run_id}/security-evidence", headers=MACHINE, json={
                "artifactDigest": body["artifactDigest"],
                "artifactRef": f"registry.local/orders@{body['artifactDigest']}",
                "sbom": {"generatedBy": "syft", "location": "s3://evidence/sbom.json", "format": "cyclonedx-json"},
                "vulnerabilityScan": {"scanner": "trivy", "status": "passed", "critical": 0, "high": 0, "medium": 0},
                "signature": {"provider": "cosign", "verified": True, "certificateIdentity": "netci"},
            })
            assert published.status_code == 202, published.text
    return client.post(f"/pipeline-runs/{run_id}/ci-result", headers=MACHINE, json=body)


def _healthy(deployment_id):
    response = client.post(f"/deployments/{deployment_id}/result", headers=MACHINE,
                           json={"status": "healthy", "message": "health check passed"})
    assert response.status_code == 202, response.text


def _deployments(application_id):
    return client.get("/deployments", params={"applicationId": application_id}).json()["items"]


def test_a_push_to_main_builds_and_deploys_to_dev():
    module, repo = _module()
    hook = _push(repo, "refs/heads/main")
    assert hook.status_code == 201, hook.text
    assert hook.json()["decision"]["deployTo"] == "dev"

    run = client.get(f"/pipeline-runs/{hook.json()['pipelineRunId']}").json()
    assert (run["deployAfterBuild"], run["environment"], run["trigger"]["event"]) == (True, "dev", "push")
    # Webhook runs now get the module's server-managed target, like the Run button.
    assert run["parameters"]["target_hosts"] == ["dev-host"]

    done = _succeed(run["id"]).json()
    assert done["deployment"]["environment"] == "dev"


def test_a_push_to_another_branch_is_built_and_not_deployed():
    module, repo = _module()
    run_id = _push(repo, "refs/heads/feature/login").json()["pipelineRunId"]

    done = _succeed(run_id)
    assert done.status_code == 202, done.text
    assert done.json()["deployment"] is None
    assert done.json()["pipelineRun"]["status"] == "succeeded"
    assert done.json()["pipelineRun"]["artifactDigest"].startswith("sha256:")
    assert _deployments(module["applicationId"]) == []


def test_a_pull_request_from_the_repository_is_built_published_and_not_deployed():
    module, repo = _module()
    hook = _pull_request(repo)
    run = client.get(f"/pipeline-runs/{hook.json()['pipelineRunId']}").json()
    assert (run["deployAfterBuild"], run["publishArtifact"]) == (False, True)
    assert run["trigger"]["pullRequest"] == 7 and run["trigger"]["fromFork"] is False


def test_a_fork_pull_request_is_ignored_by_default():
    _, repo = _module()
    hook = _pull_request(repo, fork=True)
    assert hook.status_code == 200
    assert hook.json()["status"] == "ignored" and "fork" in hook.json()["reason"]


def test_a_fork_pull_request_the_module_opts_into_is_verify_only_and_may_not_report_a_digest():
    module, repo = _module(delivery={"forkPullRequests": "verify"})
    run_id = _pull_request(repo, fork=True).json()["pipelineRunId"]
    run = client.get(f"/pipeline-runs/{run_id}").json()
    assert run["publishArtifact"] is False

    # The fork's own code controls what the build reports. A digest from it is refused.
    forged = _succeed(run_id, digest=f"sha256:{'e' * 64}", evidence=False)
    assert forged.status_code == 422
    assert forged.json()["code"] == "UNPUBLISHED_RUN_HAS_NO_ARTIFACT"

    verified = client.post(f"/pipeline-runs/{run_id}/ci-result", headers=MACHINE, json={"status": "succeeded"})
    assert verified.status_code == 202, verified.text
    assert verified.json()["pipelineRun"]["artifactDigest"] is None

    # Nothing it built can be promoted.
    promoted = client.post(f"/modules/{module['id']}/promotions", json={"pipelineRunId": run_id, "environment": "dev"})
    assert promoted.status_code == 409 and promoted.json()["code"] == "NO_DEPLOYABLE_ARTIFACT"


def test_a_retried_verify_only_run_stays_verify_only():
    _, repo = _module(delivery={"forkPullRequests": "verify"})
    run_id = _pull_request(repo, fork=True).json()["pipelineRunId"]
    client.post(f"/pipeline-runs/{run_id}/ci-result", headers=MACHINE, json={"status": "running"})
    client.post(f"/pipeline-runs/{run_id}/ci-result", headers=MACHINE, json={"status": "failed"})
    retried = client.post(f"/pipeline-runs/{run_id}/retry", json={})
    assert retried.status_code in (200, 201, 202), retried.text
    assert retried.json()["publishArtifact"] is False and retried.json()["deployAfterBuild"] is False


def test_a_version_tag_becomes_a_version_in_the_same_transaction_as_the_build():
    module, repo = _module()
    run_id = _push(repo, "refs/tags/v1.3.0").json()["pipelineRunId"]
    run = client.get(f"/pipeline-runs/{run_id}").json()
    assert run["releaseTag"] == "v1.3.0" and run["deployAfterBuild"] is False

    digest = f"sha256:{'c' * 64}"
    _succeed(run_id, digest=digest)
    versions = client.get(f"/modules/{module['id']}/versions").json()
    items = versions["items"] if isinstance(versions, dict) else versions
    registered = [v for v in items if v.get("version") == "v1.3.0"]
    assert registered and registered[0]["artifactDigest"] == digest
    assert registered[0]["pipelineRunId"] == run_id


def test_a_tag_that_is_not_a_version_starts_nothing_under_the_default_rules():
    _, repo = _module()
    assert _push(repo, "refs/tags/nightly").json()["status"] == "ignored"


def test_promotion_moves_the_built_digest_and_staging_needs_it_healthy_in_dev_first():
    module, repo = _module()
    run_id = _push(repo, "refs/heads/feature/pay").json()["pipelineRunId"]
    digest = _succeed(run_id).json()["pipelineRun"]["artifactDigest"]

    early = client.post(f"/modules/{module['id']}/promotions", json={"pipelineRunId": run_id, "environment": "staging"})
    assert early.status_code == 409 and early.json()["code"] == "PROMOTION_SOURCE_NOT_HEALTHY"

    to_dev = client.post(f"/modules/{module['id']}/promotions", json={"pipelineRunId": run_id, "environment": "dev"})
    assert to_dev.status_code == 202, to_dev.text
    assert to_dev.json()["artifactDigest"] == digest
    _healthy(to_dev.json()["deploymentId"])

    to_staging = client.post(f"/modules/{module['id']}/promotions", json={"pipelineRunId": run_id, "environment": "staging"})
    assert to_staging.status_code == 202, to_staging.text
    assert to_staging.json()["artifactDigest"] == digest
    assert "healthy in dev" in to_staging.json()["evidence"]


def test_a_soak_the_module_requires_is_enforced():
    module, repo = _module(delivery={"promotion": {"staging": {"requireHealthyIn": "dev", "minSoakMinutes": 30}}})
    run_id = _push(repo, "refs/heads/main").json()["pipelineRunId"]
    deployment = _succeed(run_id).json()["deployment"]
    _healthy(deployment["id"])

    refused = client.post(f"/modules/{module['id']}/promotions", json={"pipelineRunId": run_id, "environment": "staging"})
    assert refused.status_code == 409
    assert refused.json()["code"] == "PROMOTION_SOAK_NOT_MET"
    assert "30 min" in refused.json()["message"]


def test_production_is_never_reached_by_promotion_or_by_a_trigger():
    module, repo = _module()
    run_id = _push(repo, "refs/heads/main").json()["pipelineRunId"]
    _succeed(run_id)
    to_prod = client.post(f"/modules/{module['id']}/promotions", json={"pipelineRunId": run_id, "environment": "prod"})
    assert to_prod.status_code == 422 and to_prod.json()["code"] == "PRODUCTION_REQUIRES_REQUEST"

    system = f"sys-{uuid4().hex[:6]}"
    client.post("/systems", json={"id": system, "unit": "Unit", "description": "desc"})
    refused = client.post(f"/systems/{system}/modules", json={
        "name": "prod-trigger", "displayName": "p", "repositoryUrl": "https://github.com/acme/p",
        "pipelineTemplate": "container-ci-cd-v1", "runtime": "docker", "moduleType": "Backend",
        "description": "d",
        "deploymentEnvironments": [{"displayName": "prod", "environment": "prod", "runtime": "docker",
                                    "servers": ["prod-host"]}],
        "defaultEnvironment": "prod",
        "pipelineConfig": {"runner": "jenkins", "strategy": "Trunk-based", "pipelines": {"ci": {"branch": "main", "stages": ["build"]}},
                           "delivery": {"triggers": [{"on": "push", "branches": ["main"], "deployTo": "prod"}]}},
    })
    assert refused.status_code == 422, refused.text
    assert refused.json()["code"] == "INVALID_DELIVERY_RULES"


def test_a_production_request_honours_the_prod_promotion_rule():
    module, repo = _module(delivery={"promotion": {"prod": {"requireHealthyIn": "staging"}}})
    run_id = _push(repo, "refs/tags/v2.0.0").json()["pipelineRunId"]
    _succeed(run_id)

    request = client.post("/production-requests", json={
        "modules": [{"moduleId": module["id"], "version": "v2.0.0"}],
        "scheduledFor": "2026-09-24T10:00:00Z",
    })
    assert request.status_code == 409, request.text
    assert request.json()["code"] == "PROMOTION_SOURCE_NOT_HEALTHY"


def test_changing_the_prod_promotion_rule_needs_a_second_person(monkeypatch):
    monkeypatch.setenv("NETCI_REQUIRE_PRODUCTION_CONFIG_APPROVAL", "true")
    module, _ = _module()
    detail = client.get(f"/modules/{module['id']}").json()
    targets = [{"environment": e["environment"], "runtime": "docker", "servers": e.get("servers", [])}
               for e in detail["deploymentEnvironments"]]
    revision = client.post(f"/modules/{module['id']}/config-revisions", json={
        "changeSummary": "stop requiring staging",
        "pipelineConfig": {"delivery": {"promotion": {"prod": {"requireHealthyIn": "staging", "minSoakMinutes": 60}}}},
        "deploymentConfig": targets,
    })
    assert revision.status_code == 201, revision.text
    assert revision.json()["status"] == "pending_approval"


def test_the_module_build_inputs_reach_a_webhook_run():
    _, repo = _module(build_inputs={"NETCI_APP_DIR": "services/orders"})
    run_id = _push(repo, "refs/heads/feature/x").json()["pipelineRunId"]
    run = client.get(f"/pipeline-runs/{run_id}").json()
    assert run["parameters"]["NETCI_APP_DIR"] == "services/orders"


def test_a_module_build_input_that_names_a_deploy_key_is_refused():
    system = f"sys-{uuid4().hex[:6]}"
    client.post("/systems", json={"id": system, "unit": "Unit", "description": "desc"})
    refused = client.post(f"/systems/{system}/modules", json={
        "name": "sneaky", "displayName": "s", "repositoryUrl": "https://github.com/acme/s",
        "pipelineTemplate": "container-ci-cd-v1", "runtime": "docker", "moduleType": "Backend",
        "description": "d",
        "deploymentEnvironments": [{"displayName": "dev", "environment": "dev", "runtime": "docker",
                                    "servers": ["dev-host"]}],
        "pipelineConfig": {"runner": "jenkins", "strategy": "Trunk-based", "pipelines": {"ci": {"branch": "main", "stages": ["build"]}},
                           "buildInputs": {"target_hosts": ["attacker"]}},
    })
    assert refused.status_code == 422, refused.text
    assert refused.json()["code"] == "INVALID_BUILD_INPUTS"


def test_a_manual_build_only_run_creates_no_deployment():
    module, _ = _module()
    run = client.post(f"/modules/{module['id']}/pipeline-runs",
                      json={"commitSha": "abcdef1234567", "branch": "main", "environment": "dev", "deploy": False})
    assert run.status_code == 202, run.text
    assert run.json()["deployAfterBuild"] is False and run.json()["trigger"]["event"] == "manual"
    assert _succeed(run.json()["id"]).json()["deployment"] is None


def test_the_effective_rules_are_readable():
    module, _ = _module()
    rules = client.get(f"/modules/{module['id']}/delivery-rules").json()
    assert rules["defaulted"] is True
    assert rules["triggers"][0] == {"on": "push", "branches": ["main"], "deployTo": "dev"}
    assert rules["forkPullRequests"] == "ignore"
    assert UUID  # imported for readers who follow the ids
