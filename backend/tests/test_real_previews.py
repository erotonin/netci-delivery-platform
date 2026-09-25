"""Pull-request preview environments are real deployments (ADR-049).

Before this, `PreviewEnvironmentManager.create_preview` wrote "active" with a made-up URL
and deployed nothing -- exactly the fake-success this project forbids. These tests drive
the trigger, the callback and the teardown paths the way a same-repository pull request,
its worker and its closing actually would, through the real API.
"""

from __future__ import annotations

import re
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

import app.main as main_mod
from app import workload_identity
from app.adapters.scm import MockScmProvider, get_scm_provider, set_scm_provider
from app.domain.models import ScmProviderType
from app.workload_identity import Scope, Workload

from test_ci_cd_separation import SECRET, _hook, _pull_request, _succeed

client = TestClient(main_mod.app)
MACHINE = {"Authorization": "Bearer netci-local-pipeline-key"}
TOKEN_KEYS = "k1:" + "p" * 48


@pytest.fixture(autouse=True)
def fresh_platform(monkeypatch):
    monkeypatch.setenv("NETCI_WORKLOAD_TOKEN_KEYS", TOKEN_KEYS)
    monkeypatch.delenv("NETCI_WORKLOAD_TOKEN_KEYS_FILE", raising=False)
    main_mod.platform.reset()
    original = get_scm_provider(ScmProviderType.GITHUB)
    set_scm_provider(ScmProviderType.GITHUB, MockScmProvider(ScmProviderType.GITHUB))
    yield
    set_scm_provider(ScmProviderType.GITHUB, original)


def _kubernetes_module(
    name="checkout",
    *,
    previews: dict | None = {"enabled": True, "ttlHours": 2},
    delivery: dict | None = None,
    environments=("dev", "staging", "prod"),
):
    system = f"sys-{uuid4().hex[:6]}"
    assert client.post(
        "/systems", json={"id": system, "unit": "Checkout", "description": "checkout"}
    ).status_code == 201
    pipeline_config = {
        "runner": "jenkins", "strategy": "Trunk-based",
        "pipelines": {"ci": {"branch": "main", "stages": ["build"]}},
    }
    if previews is not None:
        pipeline_config["previews"] = previews
    if delivery is not None:
        pipeline_config["delivery"] = delivery
    body = {
        "name": name, "displayName": name, "repositoryUrl": f"https://github.com/acme/{name}",
        "pipelineTemplate": "kubernetes-ci-cd-v1", "runtime": "kubernetes", "moduleType": "Backend",
        "description": "test module",
        "pipelineConfig": pipeline_config,
        "deploymentEnvironments": [
            {
                "displayName": env, "environment": env, "runtime": "kubernetes",
                "kubeconfigRef": f"{env}-kubeconfig", "namespace": f"ns-{env}",
            }
            for env in environments
        ],
    }
    created = client.post(f"/systems/{system}/modules", json=body)
    assert created.status_code == 201, created.text
    module = created.json()
    repo = f"acme/{name}-{uuid4().hex[:4]}"
    scm = client.post(
        f"/applications/{module['applicationId']}/scm",
        json={"provider": "github", "repositoryIdentity": repo, "secretToken": SECRET},
    )
    assert scm.status_code == 201, scm.text
    return module, repo


def _pull_request_closed(repo, *, number: int):
    return _hook("pull_request", {
        "action": "closed", "repository": {"full_name": repo}, "sender": {"login": "contributor"},
        "pull_request": {"number": number,
                         "head": {"sha": uuid4().hex + uuid4().hex[:8], "ref": "feature/x", "repo": {"full_name": repo}},
                         "base": {"ref": "main", "repo": {"full_name": repo}}},
    })


def _previews(application_id: str) -> list[dict]:
    return client.get("/preview-environments", params={"applicationId": application_id}).json()["items"]


def _preview_token(preview: dict, *, scopes=None) -> str:
    return workload_identity.mint(
        workload=Workload.TEMPORAL,
        application_id=UUID(preview["applicationId"]),
        pipeline_run_id=UUID(preview["pipelineRunId"]),
        scopes=scopes or {Scope.PREVIEW_RESULT, Scope.CI_EVIDENCE},
    )


def test_pull_request_success_starts_a_deploying_preview_never_active():
    module, repo = _kubernetes_module()
    hook = _pull_request(repo, number=42)
    assert hook.status_code == 201, hook.text
    run_id = hook.json()["pipelineRunId"]

    result = _succeed(run_id)
    assert result.status_code == 202, result.text

    items = _previews(module["applicationId"])
    assert len(items) == 1
    preview = items[0]
    assert preview["status"] == "deploying"
    assert preview["url"] is None
    assert preview["namespace"] == f"preview-{module['id']}-pr-42"
    assert preview["releaseName"] == f"{module['id']}-pr-42"
    assert preview["pipelineRunId"] == run_id
    assert preview["artifactDigest"] is not None


def test_a_second_push_to_the_same_pr_redeploys_the_same_preview():
    module, repo = _kubernetes_module()
    first_run = _pull_request(repo, number=9).json()["pipelineRunId"]
    _succeed(first_run)
    first = _previews(module["applicationId"])[0]

    second_run = _pull_request(repo, number=9).json()["pipelineRunId"]
    _succeed(second_run)

    items = _previews(module["applicationId"])
    assert len(items) == 1
    second = items[0]
    assert second["previewId"] == first["previewId"]
    assert second["namespace"] == first["namespace"]
    assert second["pipelineRunId"] == second_run
    assert second["status"] == "deploying"


def test_fork_pull_request_never_gets_a_preview():
    module, repo = _kubernetes_module(delivery={"forkPullRequests": "verify"})
    run_id = _pull_request(repo, fork=True, number=11).json()["pipelineRunId"]

    result = _succeed(run_id, digest=False)
    assert result.status_code == 202, result.text
    assert result.json()["pipelineRun"]["publishArtifact"] is False

    assert _previews(module["applicationId"]) == []


def test_invalid_previews_config_is_422_when_the_module_is_not_kubernetes():
    system = f"sys-{uuid4().hex[:6]}"
    assert client.post("/systems", json={"id": system, "unit": "Orders", "description": "orders"}).status_code == 201
    body = {
        "name": "orders-docker", "displayName": "orders-docker",
        "repositoryUrl": "https://github.com/acme/orders-docker",
        "pipelineTemplate": "container-ci-cd-v1", "runtime": "docker", "moduleType": "Backend",
        "pipelineConfig": {
            "runner": "jenkins", "strategy": "Trunk-based",
            "pipelines": {"ci": {"branch": "main", "stages": ["build"]}},
            "previews": {"enabled": True},
        },
        "deploymentEnvironments": [
            {"displayName": "dev", "environment": "dev", "runtime": "docker", "servers": ["dev-host"]},
        ],
    }
    created = client.post(f"/systems/{system}/modules", json=body)
    assert created.status_code == 422, created.text
    assert created.json()["code"] == "INVALID_PREVIEWS"


def test_invalid_previews_config_is_422_without_a_dev_target():
    """No `dev` deployment target at all -- `defaultEnvironment` is set to `staging` so
    the module itself is otherwise valid (ModuleCreate requires the default among the
    declared environments); previews still refuses it."""

    system = f"sys-{uuid4().hex[:6]}"
    assert client.post("/systems", json={"id": system, "unit": "Orders", "description": "orders"}).status_code == 201
    body = {
        "name": "orders-k8s", "displayName": "orders-k8s",
        "repositoryUrl": "https://github.com/acme/orders-k8s",
        "pipelineTemplate": "kubernetes-ci-cd-v1", "runtime": "kubernetes", "moduleType": "Backend",
        "defaultEnvironment": "staging",
        "pipelineConfig": {
            "runner": "jenkins", "strategy": "Trunk-based",
            "pipelines": {"ci": {"branch": "main", "stages": ["build"]}},
            "previews": {"enabled": True},
        },
        "deploymentEnvironments": [
            {"displayName": "staging", "environment": "staging", "runtime": "kubernetes",
             "kubeconfigRef": "staging-kubeconfig", "namespace": "orders-staging"},
        ],
    }
    created = client.post(f"/systems/{system}/modules", json=body)
    assert created.status_code == 422, created.text
    assert created.json()["code"] == "INVALID_PREVIEWS"


def test_namespace_and_release_stay_within_limits_for_a_long_module_id():
    long_name = "a" * 60 + "-svc"  # 64 chars total is invalid; keep under ModuleCreate's 63
    long_name = long_name[:60]
    module, repo = _kubernetes_module(name=long_name)
    run_id = _pull_request(repo, number=123456789).json()["pipelineRunId"]
    _succeed(run_id)

    preview = _previews(module["applicationId"])[0]
    assert len(preview["namespace"]) <= 63
    assert len(preview["releaseName"]) <= 63
    assert re.fullmatch(r"preview-[a-z0-9]([a-z0-9-]{0,38}[a-z0-9])?-pr-[0-9]{1,9}", preview["namespace"])
    assert re.fullmatch(r"[a-z0-9]([a-z0-9-]{0,38}[a-z0-9])?-pr-[0-9]{1,9}", preview["releaseName"])


def test_callback_with_wrong_scope_is_403():
    module, repo = _kubernetes_module()
    run_id = _pull_request(repo, number=5).json()["pipelineRunId"]
    _succeed(run_id)
    preview = _previews(module["applicationId"])[0]

    wrong_scope = _preview_token(preview, scopes={Scope.CI_EVIDENCE})
    refused = client.post(
        f"/preview-environments/{preview['previewId']}/result",
        json={"status": "active", "url": "https://x.example"},
        headers={"Authorization": f"Bearer {wrong_scope}"},
    )
    assert refused.status_code == 403
    assert refused.json()["code"] == "SCOPE_NOT_PERMITTED"
    assert client.get(f"/preview-environments/{preview['previewId']}").json()["status"] == "deploying"


def test_callback_with_another_runs_token_is_403():
    module, repo = _kubernetes_module()
    run_a = _pull_request(repo, number=6).json()["pipelineRunId"]
    _succeed(run_a)
    preview = _previews(module["applicationId"])[0]

    other_run = _pull_request(repo, number=7).json()["pipelineRunId"]
    other_token = workload_identity.mint(
        workload=Workload.TEMPORAL, application_id=UUID(module["applicationId"]),
        pipeline_run_id=UUID(other_run), scopes={Scope.PREVIEW_RESULT},
    )
    refused = client.post(
        f"/preview-environments/{preview['previewId']}/result",
        json={"status": "active", "url": "https://x.example"},
        headers={"Authorization": f"Bearer {other_token}"},
    )
    assert refused.status_code == 403
    assert refused.json()["code"] == "RESOURCE_MISMATCH"


def test_deploying_transitions_to_active_with_the_reported_url():
    module, repo = _kubernetes_module()
    run_id = _pull_request(repo, number=21).json()["pipelineRunId"]
    _succeed(run_id)
    preview = _previews(module["applicationId"])[0]
    token = _preview_token(preview)

    accepted = client.post(
        f"/preview-environments/{preview['previewId']}/result",
        json={"status": "active", "url": "https://checkout-pr-21.preview.local", "message": "deployed"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert accepted.status_code == 202, accepted.text
    body = accepted.json()
    assert body["status"] == "active"
    assert body["url"] == "https://checkout-pr-21.preview.local"


def test_a_result_without_a_url_stays_null_never_invented():
    module, repo = _kubernetes_module()
    run_id = _pull_request(repo, number=22).json()["pipelineRunId"]
    _succeed(run_id)
    preview = _previews(module["applicationId"])[0]
    token = _preview_token(preview)

    accepted = client.post(
        f"/preview-environments/{preview['previewId']}/result",
        json={"status": "active", "message": "no ingress was found"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert accepted.status_code == 202, accepted.text
    assert accepted.json()["url"] is None


def test_teardown_via_pull_request_closed_webhook():
    module, repo = _kubernetes_module()
    run_id = _pull_request(repo, number=30).json()["pipelineRunId"]
    _succeed(run_id)
    preview = _previews(module["applicationId"])[0]
    token = _preview_token(preview)
    client.post(
        f"/preview-environments/{preview['previewId']}/result",
        json={"status": "active", "url": "https://x.example"},
        headers={"Authorization": f"Bearer {token}"},
    )

    closed = _pull_request_closed(repo, number=30)
    assert closed.status_code == 200, closed.text
    assert closed.json()["status"] == "preview_teardown"

    after = client.get(f"/preview-environments/{preview['previewId']}").json()
    assert after["status"] == "destroying"

    # Closing again finds nothing live to tear down.
    ignored = _pull_request_closed(repo, number=30)
    assert ignored.json()["status"] == "ignored"


def test_teardown_result_is_destroyed():
    module, repo = _kubernetes_module()
    run_id = _pull_request(repo, number=31).json()["pipelineRunId"]
    _succeed(run_id)
    preview = _previews(module["applicationId"])[0]
    client.post(
        f"/preview-environments/{preview['previewId']}/result",
        json={"status": "active", "url": "https://x.example"},
        headers={"Authorization": f"Bearer {_preview_token(preview)}"},
    )

    teardown = client.post(f"/preview-environments/{preview['previewId']}/teardown")
    assert teardown.status_code == 200, teardown.text
    assert teardown.json()["status"] == "destroying"

    # Reuse a fresh token bound to the same pipeline run for the teardown's own report.
    destroyed = client.post(
        f"/preview-environments/{preview['previewId']}/result",
        json={"status": "destroyed", "message": "namespace removed"},
        headers={"Authorization": f"Bearer {_preview_token(preview)}"},
    )
    assert destroyed.status_code == 202, destroyed.text
    assert destroyed.json()["status"] == "destroyed"
    assert destroyed.json()["destroyedAt"] is not None


def test_expiry_reaper_moves_expired_previews_to_destroying_and_starts_teardown():
    module, repo = _kubernetes_module(previews={"enabled": True, "ttlHours": 1})
    run_id = _pull_request(repo, number=40).json()["pipelineRunId"]
    _succeed(run_id)
    preview = _previews(module["applicationId"])[0]
    client.post(
        f"/preview-environments/{preview['previewId']}/result",
        json={"status": "active", "url": "https://x.example"},
        headers={"Authorization": f"Bearer {_preview_token(preview)}"},
    )
    with main_mod.database.transaction() as session:
        record = session.preview_environment(preview["previewId"])
        session.update_preview_environment(
            record.id, status=record.status, detail=record.detail, url=record.url,
            expires_at=record.created_at,
        )

    outcome = main_mod._reap_expired_previews()

    assert outcome["reaped"] == 1
    after = client.get(f"/preview-environments/{preview['previewId']}").json()
    assert after["status"] == "destroying"
    assert after["detail"].startswith("ttl expired")

    expired = client.post(
        f"/preview-environments/{preview['previewId']}/result",
        json={"status": "destroyed", "message": "namespace removed"},
        headers={"Authorization": f"Bearer {_preview_token(preview)}"},
    )
    assert expired.json()["status"] == "expired"


def test_slug_does_not_collide_for_long_module_ids_with_common_prefix():
    from app.catalog.previews import preview_names
    mod1 = "telecom-service-subscriber-authentication-v1"
    mod2 = "telecom-service-subscriber-authentication-v2"
    ns1, rel1 = preview_names(mod1, 42)
    ns2, rel2 = preview_names(mod2, 42)
    assert rel1 != rel2, "Releases must not collide for modules sharing a common prefix"
    assert ns1 != ns2, "Namespaces must not collide for modules sharing a common prefix"
    # Ensure both fit within 63 characters and match the playbook regex
    k8s_ns_regex = re.compile(r"^preview-[a-z0-9]([a-z0-9-]{0,38}[a-z0-9])?-pr-[0-9]{1,9}$")
    assert k8s_ns_regex.match(ns1), f"Namespace '{ns1}' does not match K8s playbook regex"
    assert k8s_ns_regex.match(ns2), f"Namespace '{ns2}' does not match K8s playbook regex"
    assert len(ns1) <= 63
    assert len(ns2) <= 63


def test_record_result_fails_with_409_if_preview_was_concurrently_moved_to_destroying():
    module, repo = _kubernetes_module()
    run_id = _pull_request(repo, number=55).json()["pipelineRunId"]
    _succeed(run_id)
    preview = _previews(module["applicationId"])[0]

    # Teardown starts (e.g. PR closed or explicit teardown)
    with main_mod.database.transaction() as session:
        session.update_preview_environment(preview["previewId"], status="destroying", detail="teardown requested")

    # Stale deploy worker attempts to report active
    late_deploy = client.post(
        f"/preview-environments/{preview['previewId']}/result",
        json={"status": "active", "url": "https://late.example"},
        headers={"Authorization": f"Bearer {_preview_token(preview)}"},
    )
    assert late_deploy.status_code == 409
    assert late_deploy.json()["code"] == "INVALID_PREVIEW_STATE"


def test_preview_hook_exception_does_not_rollback_ci_success(monkeypatch):
    module, repo = _kubernetes_module()
    run_id = _pull_request(repo, number=77).json()["pipelineRunId"]

    # Sabotage preview_hook so it throws an unexpected exception
    def broken_preview(*args, **kwargs):
        raise RuntimeError("simulated unexpected preview error")

    monkeypatch.setattr(main_mod.platform, "preview_hook", broken_preview)

    # Build must still succeed with 202
    result = _succeed(run_id)
    assert result.status_code == 202, result.text
    run_status = client.get(f"/pipeline-runs/{run_id}").json()
    assert run_status["status"] == "succeeded"



def test_a_preview_url_that_is_not_http_is_refused_because_the_portal_renders_it_as_a_link():
    module, repo = _kubernetes_module()
    run_id = _pull_request(repo, number=23).json()["pipelineRunId"]
    _succeed(run_id)
    preview = _previews(module["applicationId"])[0]

    refused = client.post(
        f"/preview-environments/{preview['previewId']}/result",
        json={"status": "active", "url": "javascript:alert(document.cookie)"},
        headers={"Authorization": f"Bearer {_preview_token(preview)}"},
    )

    assert refused.status_code == 422, refused.text
    assert _previews(module["applicationId"])[0]["status"] == "deploying"
