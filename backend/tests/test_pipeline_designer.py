"""Tests for the pipeline designer (ADR-057).

Covers pure domain logic (ordering, required stages, ID validation, code limits,
YAML round-trip and strict parsing, builtin code directory traversal protection)
and API endpoints with a mocked GitLab client.
"""

from __future__ import annotations

import hashlib
from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from app.adapters import gitlab_repo
from app.adapters.gitlab_repo import GitLabRepoError
from app.domain.models import ScmIntegration, ScmProviderType, StageDefinition
from app.policy.rules import Role
import app.main as main
from app.pipeline_designer import (
    MAX_STAGE_CODE_BYTES,
    PipelineProposalError,
    builtin_stage_code,
    builtin_stage_path,
    current_pipeline,
    parse_pipeline_yaml,
    render_pipeline_yaml,
    validate_proposal,
)
from app.stage_catalog import BUILTIN_STAGES


# -----------------------------------------------------------------------------
# Domain tests
# -----------------------------------------------------------------------------

TEMPLATE_STAGES = (
    "checkout",
    "unit-test",
    "build",
    "sbom",
    "vulnerability-scan",
    "sign",
    "publish",
    "deploy",
    "health-check",
)


def catalog_map(*extra: StageDefinition) -> dict[str, StageDefinition]:
    items = {s.id: s for s in BUILTIN_STAGES}
    for item in extra:
        items[item.id] = item
    return items


def test_current_pipeline_order_with_anchored_custom_stages():
    customs = [
        {"id": "lint", "name": "Lint", "after": "unit-test", "script": ".netci/stages/lint.sh"},
        {"id": "perf", "name": "Performance", "after": "unit-test", "script": ".netci/stages/perf.sh"},
        {"id": "notify", "name": "Notify", "after": "publish", "script": ".netci/stages/notify.sh"},
    ]
    app_stages = ["checkout", "unit-test", "build", "sbom", "vulnerability-scan", "sign", "publish"]
    resolved = current_pipeline(app_stages, catalog_map(), customs)

    ids = [s["id"] for s in resolved]
    assert ids == [
        "checkout",
        "unit-test",
        "lint",
        "perf",
        "build",
        "sbom",
        "vulnerability-scan",
        "sign",
        "publish",
        "notify",
    ]
    # Check attributes of builtin and custom stages
    builtin_stage = next(s for s in resolved if s["id"] == "unit-test")
    assert builtin_stage["kind"] == "builtin"
    assert builtin_stage["after"] is None
    assert builtin_stage["script"] is None

    custom_stage = next(s for s in resolved if s["id"] == "lint")
    assert custom_stage["kind"] == "custom"
    assert custom_stage["after"] == "unit-test"
    assert custom_stage["script"] == ".netci/stages/lint.sh"


def test_validate_proposal_ordering_preserves_template_order():
    catalog = catalog_map()
    stages = [
        {"id": "checkout"},
        {"id": "unit-test"},
        {"id": "lint", "name": "Linter", "after": "unit-test", "code": "echo lint"},
        {"id": "build"},
        {"id": "sbom"},
        {"id": "vulnerability-scan"},
        {"id": "sign"},
        {"id": "publish"},
    ]
    validated = validate_proposal(catalog, TEMPLATE_STAGES, stages)
    assert [s["id"] for s in validated] == [
        "checkout",
        "unit-test",
        "lint",
        "build",
        "sbom",
        "vulnerability-scan",
        "sign",
        "publish",
    ]


def test_validate_proposal_reordering_builtins_refused():
    catalog = catalog_map()
    # Inverted build and unit-test
    stages = [
        {"id": "checkout"},
        {"id": "build"},
        {"id": "unit-test"},
        {"id": "sbom"},
        {"id": "vulnerability-scan"},
        {"id": "sign"},
        {"id": "publish"},
    ]
    with pytest.raises(PipelineProposalError) as exc:
        validate_proposal(catalog, TEMPLATE_STAGES, stages)
    assert exc.value.code == "PIPELINE_ORDER_INVALID"


def test_validate_proposal_dropping_optional_builtins_allowed():
    catalog = catalog_map()
    # Dropped optional "unit-test"
    stages = [
        {"id": "checkout"},
        {"id": "build"},
        {"id": "sbom"},
        {"id": "vulnerability-scan"},
        {"id": "sign"},
        {"id": "publish"},
    ]
    validated = validate_proposal(catalog, TEMPLATE_STAGES, stages)
    assert [s["id"] for s in validated] == [
        "checkout",
        "build",
        "sbom",
        "vulnerability-scan",
        "sign",
        "publish",
    ]


def test_validate_proposal_dropping_required_builtin_refused():
    catalog = catalog_map()
    # Dropped required "sbom" and "vulnerability-scan"
    stages = [
        {"id": "checkout"},
        {"id": "unit-test"},
        {"id": "build"},
        {"id": "sign"},
        {"id": "publish"},
    ]
    with pytest.raises(PipelineProposalError) as exc:
        validate_proposal(catalog, TEMPLATE_STAGES, stages)
    assert exc.value.code == "PIPELINE_ORDER_INVALID"
    assert "sbom" in exc.value.message or "vulnerability-scan" in exc.value.message


def test_validate_proposal_empty_stages_refused():
    catalog = catalog_map()
    with pytest.raises(PipelineProposalError) as exc:
        validate_proposal(catalog, TEMPLATE_STAGES, [])
    assert exc.value.code == "PIPELINE_ORDER_INVALID"


@pytest.mark.parametrize("stage_id", [
    "123test",       # starts with digit
    "Test",          # uppercase
    "a",             # too short (len 1, min 2)
    "test_stage",    # underscore not allowed
    "a" * 42,        # too long (max 41)
    "-test",         # starts with dash
])
def test_validate_proposal_custom_stage_id_regex(stage_id):
    catalog = catalog_map()
    stages = [
        {"id": "checkout"},
        {"id": "unit-test"},
        {"id": stage_id, "after": "unit-test", "code": "echo 1"},
        {"id": "build"},
        {"id": "sbom"},
        {"id": "vulnerability-scan"},
        {"id": "sign"},
        {"id": "publish"},
    ]
    with pytest.raises(PipelineProposalError) as exc:
        validate_proposal(catalog, TEMPLATE_STAGES, stages)
    assert exc.value.code == "PIPELINE_STAGE_ID_TAKEN"


def test_validate_proposal_custom_stage_id_collides_with_catalog():
    catalog = catalog_map()
    # "build" is already in catalog
    stages = [
        {"id": "checkout"},
        {"id": "unit-test"},
        {"id": "build", "after": "unit-test", "code": "echo 1"},
        {"id": "sbom"},
        {"id": "vulnerability-scan"},
        {"id": "sign"},
        {"id": "publish"},
    ]
    with pytest.raises(PipelineProposalError) as exc:
        validate_proposal(catalog, TEMPLATE_STAGES, stages)
    # Since build is a catalog builtin, reordering it before other builtins or duplicating triggers error
    assert exc.value.code in ("PIPELINE_ORDER_INVALID", "PIPELINE_STAGE_ID_TAKEN")


def test_validate_proposal_duplicate_stage_ids():
    catalog = catalog_map()
    stages = [
        {"id": "checkout"},
        {"id": "unit-test"},
        {"id": "custom-step", "after": "unit-test", "code": "echo 1"},
        {"id": "custom-step", "after": "unit-test", "code": "echo 2"},
        {"id": "build"},
        {"id": "sbom"},
        {"id": "vulnerability-scan"},
        {"id": "sign"},
        {"id": "publish"},
    ]
    with pytest.raises(PipelineProposalError) as exc:
        validate_proposal(catalog, TEMPLATE_STAGES, stages)
    assert exc.value.code == "PIPELINE_STAGE_ID_TAKEN"


def test_validate_proposal_custom_stage_anchor_missing_or_inactive():
    catalog = catalog_map()
    # Anchor "deploy" is not in submitted built-ins
    stages = [
        {"id": "checkout"},
        {"id": "unit-test"},
        {"id": "custom-step", "after": "deploy", "code": "echo 1"},
        {"id": "build"},
        {"id": "sbom"},
        {"id": "vulnerability-scan"},
        {"id": "sign"},
        {"id": "publish"},
    ]
    with pytest.raises(PipelineProposalError) as exc:
        validate_proposal(catalog, TEMPLATE_STAGES, stages)
    assert exc.value.code == "PIPELINE_STAGE_CODE_INVALID"


def test_validate_proposal_custom_stage_code_checks():
    catalog = catalog_map()
    base_stages = [
        {"id": "checkout"},
        {"id": "unit-test"},
        {"id": "build"},
        {"id": "sbom"},
        {"id": "vulnerability-scan"},
        {"id": "sign"},
        {"id": "publish"},
    ]

    # 1. Missing code
    stages_missing_code = list(base_stages)
    stages_missing_code.insert(2, {"id": "lint", "after": "unit-test", "code": None})
    with pytest.raises(PipelineProposalError) as exc:
        validate_proposal(catalog, TEMPLATE_STAGES, stages_missing_code)
    assert exc.value.code == "PIPELINE_STAGE_CODE_INVALID"

    # 2. Code exceeding 65536 bytes
    stages_oversize = list(base_stages)
    stages_oversize.insert(2, {"id": "lint", "after": "unit-test", "code": "a" * (MAX_STAGE_CODE_BYTES + 1)})
    with pytest.raises(PipelineProposalError) as exc:
        validate_proposal(catalog, TEMPLATE_STAGES, stages_oversize)
    assert exc.value.code == "PIPELINE_STAGE_CODE_INVALID"

    # 3. NUL byte in code
    stages_nul = list(base_stages)
    stages_nul.insert(2, {"id": "lint", "after": "unit-test", "code": "echo \x00 bad"})
    with pytest.raises(PipelineProposalError) as exc:
        validate_proposal(catalog, TEMPLATE_STAGES, stages_nul)
    assert exc.value.code == "PIPELINE_STAGE_CODE_INVALID"


def test_yaml_round_trip():
    stages = [
        {"id": "checkout"},
        {"id": "unit-test"},
        {"id": "lint", "name": "Linter", "after": "unit-test", "code": "#!/bin/bash\nexit 0"},
        {"id": "build"},
    ]
    rendered = render_pipeline_yaml(stages)
    parsed = parse_pipeline_yaml(rendered)

    assert parsed["version"] == 1
    assert parsed["stages"] == [
        {"id": "checkout"},
        {"id": "unit-test"},
        {"id": "lint", "name": "Linter", "after": "unit-test"},
        {"id": "build"},
    ]


@pytest.mark.parametrize("bad_yaml, error_match", [
    ("not: a: yaml", "mapping"),
    ("version: 2\nstages: []", "Unsupported pipeline.yaml version"),
    ("version: 1\nunknown_root: true\nstages: []", "Unknown root key"),
    ("version: 1\nstages: not_a_list", "'stages' must be a list"),
    ("version: 1\nstages:\n  - 42", "must be a mapping"),
    ("version: 1\nstages:\n  - id: lint\n    extra: bad", "Unknown key"),
    ("version: 1\nstages:\n  - id: BAD_ID", "Invalid stage id"),
    ("version: 1\nstages:\n  - id: lint\n    after: BAD_ANCHOR", "Invalid 'after' stage anchor"),
])
def test_parse_pipeline_yaml_strict(bad_yaml, error_match):
    with pytest.raises(ValueError, match=error_match):
        parse_pipeline_yaml(bad_yaml)


def test_builtin_stage_code_resolution_and_traversal_prevention():
    # 1. Known builtin stage code reads real file
    code = builtin_stage_code("container-ci-cd-v1", "unit-test")
    assert "pytest" in code or len(code) > 0

    # 2. Checkout returns explanation
    checkout_code = builtin_stage_code("container-ci-cd-v1", "checkout")
    assert "Checkout is handled directly" in checkout_code

    # 3. Path traversal attempts
    assert builtin_stage_code("../../../../etc", "unit-test") == ""
    assert builtin_stage_code("container-ci-cd-v1/../../etc", "unit-test") == ""
    assert builtin_stage_code("invalid..template", "unit-test") == ""
    assert builtin_stage_code("container-ci-cd-v1", "nonexistent-stage") == ""

    # Path helper
    path = builtin_stage_path("container-ci-cd-v1", "unit-test")
    assert path == "jenkins/shared-library/resources/netci/tooling/templates/container-ci-cd-v1/scripts/ci/test.sh"


# -----------------------------------------------------------------------------
# API tests with fake GitLab
# -----------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def reset_environment():
    main.platform.reset()
    main.portal.reset()
    main.app.dependency_overrides.clear()


class FakeGitLab:
    def __init__(self) -> None:
        self.files: dict[tuple[str, str, str], str] = {}  # (project, path, ref) -> content
        self.default_branches: dict[str, str] = {}
        self.commits: list[dict[str, Any]] = []
        self.merge_requests: list[dict[str, Any]] = []
        self._next_iid = 1
        # project -> head commit of its default branch; a list is consumed one call at a
        # time (the last entry repeats), to model the branch moving mid-apply.
        self.heads: dict[str, Any] = {}

    def head_commit(self, project: str, branch: str) -> str:
        head = self.heads.get(project, "no-head")
        if isinstance(head, list):
            return head.pop(0) if len(head) > 1 else head[0]
        return head

    def get_file(self, project: str, path: str, ref: str) -> str | None:
        return self.files.get((project, path, ref))

    def default_branch(self, project: str) -> str:
        return self.default_branches.get(project, "main")

    def commit_files(
        self,
        project: str,
        branch: str,
        start_branch: str,
        message: str,
        actions: list[dict[str, str]],
    ) -> dict[str, Any]:
        self.commits.append({
            "project": project,
            "branch": branch,
            "start_branch": start_branch,
            "message": message,
            "actions": actions,
        })
        for act in actions:
            self.files[(project, act["file_path"], branch)] = act["content"]
        return {"id": "commit-" + branch, "message": message}

    def open_merge_request(
        self,
        project: str,
        source_branch: str,
        target_branch: str,
        title: str,
        description: str,
    ) -> dict[str, Any]:
        iid = self._next_iid
        self._next_iid += 1
        mr = {
            "iid": iid,
            "web_url": f"https://gitlab.com/{project}/-/merge_requests/{iid}",
            "project": project,
            "source_branch": source_branch,
            "target_branch": target_branch,
            "title": title,
            "description": description,
        }
        self.merge_requests.append(mr)
        return {"iid": mr["iid"], "web_url": mr["web_url"]}


@pytest.fixture
def fake_gitlab(monkeypatch):
    fake = FakeGitLab()
    monkeypatch.setattr(gitlab_repo, "get_file", fake.get_file)
    monkeypatch.setattr(gitlab_repo, "default_branch", fake.default_branch)
    monkeypatch.setattr(gitlab_repo, "head_commit", fake.head_commit)
    monkeypatch.setattr(gitlab_repo, "commit_files", fake.commit_files)
    monkeypatch.setattr(gitlab_repo, "open_merge_request", fake.open_merge_request)
    return fake


def configure_gitlab_scm(module_id: str, repo_identity: str = "acme/hello-container", token: str = "secret-token"):
    module = main.portal.module(module_id)
    app_uuid = UUID(str(module["applicationId"]))
    with main.database.transaction() as session:
        session.upsert_scm_integration(
            ScmIntegration(
                application_id=app_uuid,
                provider=ScmProviderType.GITLAB,
                repository_identity=repo_identity,
                secret_token=token,
                secret_token_hash=hashlib.sha256(token.encode()).hexdigest(),
                enabled=True,
            )
        )


def configure_github_scm(module_id: str, repo_identity: str = "acme/hello-github"):
    module = main.portal.module(module_id)
    app_uuid = UUID(str(module["applicationId"]))
    with main.database.transaction() as session:
        session.upsert_scm_integration(
            ScmIntegration(
                application_id=app_uuid,
                provider=ScmProviderType.GITHUB,
                repository_identity=repo_identity,
                secret_token="github-secret",
                secret_token_hash=hashlib.sha256(b"github-secret").hexdigest(),
                enabled=True,
            )
        )


def test_api_get_module_pipeline(fake_gitlab):
    client = TestClient(main.app)
    configure_gitlab_scm("hello-container", "acme/hello-container")

    resp = client.get("/modules/hello-container/pipeline")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["moduleId"] == "hello-container"
    assert data["template"] == "container-ci-cd-v1"
    assert len(data["stages"]) > 0
    assert any(s["id"] == "checkout" and s["kind"] == "builtin" for s in data["stages"])
    assert data["repository"]["provider"] == "gitlab"
    assert data["repository"]["identity"] == "acme/hello-container"
    assert data["repository"]["supportsProposals"] is True


def test_api_get_stage_code(fake_gitlab):
    client = TestClient(main.app)
    configure_gitlab_scm("hello-container", "acme/hello-container")

    # 1. Built-in stage
    resp = client.get("/modules/hello-container/pipeline/stages/unit-test/code")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["stageId"] == "unit-test"
    assert body["language"] == "bash"
    assert body["editable"] is False
    assert "test.sh" in body["path"]
    assert len(body["content"]) > 0

    # 2. Checkout
    resp = client.get("/modules/hello-container/pipeline/stages/checkout/code")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["editable"] is False
    assert "Checkout is handled directly" in body["content"]

    # 3. Custom stage registered in catalog
    main.platform.register_custom_stage(
        actor="admin",
        requires_approval=False,
        stage_id="my-custom",
        name="Custom",
        script=".netci/stages/my-custom.sh",
        after_stage="unit-test",
    )
    fake_gitlab.files[("acme/hello-container", ".netci/stages/my-custom.sh", "main")] = "#!/bin/bash\necho hello"

    resp = client.get("/modules/hello-container/pipeline/stages/my-custom/code")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["stageId"] == "my-custom"
    assert body["editable"] is True
    assert body["content"] == "#!/bin/bash\necho hello"

    # 4. Unknown stage
    resp = client.get("/modules/hello-container/pipeline/stages/unknown-stage/code")
    assert resp.status_code == 404, resp.text
    assert resp.json()["code"] == "STAGE_NOT_FOUND"


def test_api_propose_module_pipeline_success(fake_gitlab):
    client = TestClient(main.app)
    configure_gitlab_scm("hello-container", "acme/hello-container")

    payload = {
        "title": "Add code formatting check",
        "stages": [
            {"id": "checkout"},
            {"id": "unit-test"},
            {
                "id": "fmt-check",
                "name": "Format Check",
                "after": "unit-test",
                "code": "#!/bin/bash\nset -euo pipefail\necho checking formatting",
            },
            {"id": "build"},
            {"id": "sbom"},
            {"id": "vulnerability-scan"},
            {"id": "sign"},
            {"id": "publish"},
        ],
    }

    resp = client.post("/modules/hello-container/pipeline/proposals", json=payload)
    assert resp.status_code == 201, resp.text
    data = resp.json()

    assert data["branch"].startswith("netci/pipeline-")
    assert data["iid"] == 1
    assert "merge_requests/1" in data["mergeRequestUrl"]
    assert any(s["id"] == "fmt-check" for s in data["stages"])

    # Verify commits were made to fake gitlab
    assert len(fake_gitlab.commits) == 1
    commit = fake_gitlab.commits[0]
    assert commit["branch"] == data["branch"]
    assert commit["message"] == "netCI pipeline proposal: Add code formatting check"
    action_paths = [a["file_path"] for a in commit["actions"]]
    assert ".netci/pipeline.yaml" in action_paths
    assert ".netci/stages/fmt-check.sh" in action_paths

    # Verify MR created
    assert len(fake_gitlab.merge_requests) == 1
    mr = fake_gitlab.merge_requests[0]
    assert mr["title"] == "netCI pipeline: Add code formatting check"
    assert "fmt-check" in mr["description"]

    # Verify audit record was created
    with main.database.transaction() as session:
        audits = session.audit_records()
        proposal_audits = [a for a in audits if a.event_type == "pipeline.proposal_opened"]
        assert len(proposal_audits) == 1
        assert proposal_audits[0].payload["branch"] == data["branch"]


def test_api_propose_module_pipeline_extra_fields_rejected_with_422(fake_gitlab):
    client = TestClient(main.app)
    configure_gitlab_scm("hello-container", "acme/hello-container")

    # Trying to name a branch or repository in the request body
    payload = {
        "title": "Hack branch",
        "branch": "main",  # Not allowed by StrictBody
        "stages": [
            {"id": "checkout"},
            {"id": "build"},
            {"id": "sbom"},
            {"id": "vulnerability-scan"},
            {"id": "sign"},
            {"id": "publish"},
        ],
    }
    resp = client.post("/modules/hello-container/pipeline/proposals", json=payload)
    assert resp.status_code == 422


def test_api_propose_module_pipeline_github_module_unsupported(fake_gitlab):
    client = TestClient(main.app)
    configure_github_scm("hello-container", "acme/hello-container")

    payload = {
        "title": "Proposal for GitHub",
        "stages": [
            {"id": "checkout"},
            {"id": "build"},
            {"id": "sbom"},
            {"id": "vulnerability-scan"},
            {"id": "sign"},
            {"id": "publish"},
        ],
    }
    resp = client.post("/modules/hello-container/pipeline/proposals", json=payload)
    assert resp.status_code == 422
    assert resp.json()["code"] == "PIPELINE_PROPOSALS_UNSUPPORTED"


def test_api_propose_module_pipeline_non_member_forbidden(fake_gitlab, monkeypatch):
    from app.auth import Principal

    # The fixture module is unowned, and unowned is open until ownership is required.
    monkeypatch.setenv("NETCI_REQUIRE_APPLICATION_OWNER", "true")

    # Not a platform-admin, so an unowned application is out of reach
    outsider = Principal(
        subject="outsider",
        display_name="Outsider Developer",
        email="outsider@example.com",
        roles=frozenset({Role.DEVELOPER}),
        method="token",
        teams=frozenset({"Frontend"}),
    )
    main.app.dependency_overrides[main.current_principal] = lambda: outsider

    client = TestClient(main.app)
    configure_gitlab_scm("hello-container", "acme/hello-container")

    payload = {
        "stages": [
            {"id": "checkout"},
            {"id": "build"},
            {"id": "sbom"},
            {"id": "vulnerability-scan"},
            {"id": "sign"},
            {"id": "publish"},
        ],
    }
    resp = client.post("/modules/hello-container/pipeline/proposals", json=payload)
    assert resp.status_code == 403
    assert resp.json()["code"] == "APPLICATION_FORBIDDEN"


def test_api_webhook_merge_applies_pipeline_and_is_idempotent(fake_gitlab):
    client = TestClient(main.app)
    token = "secret-webhook-token"
    repo_identity = "acme/hello-container"
    configure_gitlab_scm("hello-container", repo_identity, token=token)

    # Prepare merged proposal in fake gitlab
    merge_sha = "f" * 40
    proposal_yaml = render_pipeline_yaml([
        {"id": "checkout"},
        {"id": "unit-test"},
        {"id": "post-test", "name": "Post Test", "after": "unit-test"},
        {"id": "build"},
        {"id": "sbom"},
        {"id": "vulnerability-scan"},
        {"id": "sign"},
        {"id": "publish"},
    ])
    fake_gitlab.files[(repo_identity, ".netci/pipeline.yaml", merge_sha)] = proposal_yaml
    fake_gitlab.heads[repo_identity] = merge_sha

    webhook_payload = {
        "object_kind": "merge_request",
        "project": {
            "path_with_namespace": repo_identity,
            "name": "hello-container",
        },
        "user_username": "reviewer-pat",
        "object_attributes": {
            "action": "merge",
            "id": 1001,
            "iid": 42,
            "target_branch": "main",
            "source_branch": "netci/pipeline-abcdef12",
            "merge_commit_sha": merge_sha,
            "last_commit": {
                "id": "e" * 40,
            },
        },
    }

    headers = {
        "X-Gitlab-Event": "Merge Request Hook",
        "X-Gitlab-Delivery": "delivery-merge-1",
        "X-Gitlab-Token": token,
        "Content-Type": "application/json",
    }

    # First delivery
    resp1 = client.post("/webhooks/scm/gitlab", json=webhook_payload, headers=headers)
    assert resp1.status_code == 200, resp1.text
    assert resp1.json()["status"] == "pipeline_applied"

    # Verify application stages and module pipelineConfig were updated
    mod = main.portal.module("hello-container")
    app_uuid = UUID(str(mod["applicationId"]))
    app = main.platform.get_application(app_uuid)
    assert "post-test" in app.stages

    custom_stages = mod.get("pipelineConfig", {}).get("customStages", [])
    assert any(c["id"] == "post-test" for c in custom_stages)
    assert custom_stages[0]["script"] == ".netci/stages/post-test.sh"
    assert custom_stages[0]["after"] == "unit-test"

    # Second delivery with same delivery ID -> ignored duplicate
    resp2 = client.post("/webhooks/scm/gitlab", json=webhook_payload, headers=headers)
    assert resp2.status_code == 200
    assert resp2.json()["status"] == "ignored_duplicate"

    # Third delivery with new delivery ID -> idempotent (no change to stages/revisions)
    headers["X-Gitlab-Delivery"] = "delivery-merge-2"
    resp3 = client.post("/webhooks/scm/gitlab", json=webhook_payload, headers=headers)
    assert resp3.status_code == 200
    assert resp3.json()["status"] == "pipeline_applied"

    # Check that customStages and application stages remain identical
    mod_after = main.portal.module("hello-container")
    assert mod_after.get("pipelineConfig", {}).get("customStages") == custom_stages


def test_api_custom_stage_reaches_ci_launcher(fake_gitlab, monkeypatch):
    from app.adapters.ci_launcher import CiLaunchRequest, LaunchedCi

    captured: list[CiLaunchRequest] = []

    class Launcher:
        mode = "recording"

        def launch(self, request):
            captured.append(request)
            return LaunchedCi(controller_id="jenkins-a", external_run_id="job#1", console_url="http://j/1")

    monkeypatch.setattr(main.platform, "ci_launcher", Launcher())

    client = TestClient(main.app)
    token = "secret-webhook-token"
    repo_identity = "acme/hello-container"
    configure_gitlab_scm("hello-container", repo_identity, token=token)

    # Merge proposal with custom stage
    merge_sha = "a" * 40
    proposal_yaml = render_pipeline_yaml([
        {"id": "checkout"},
        {"id": "unit-test"},
        {"id": "post-test", "name": "Post Test", "after": "unit-test"},
        {"id": "build"},
        {"id": "sbom"},
        {"id": "vulnerability-scan"},
        {"id": "sign"},
        {"id": "publish"},
    ])
    fake_gitlab.files[(repo_identity, ".netci/pipeline.yaml", merge_sha)] = proposal_yaml
    fake_gitlab.heads[repo_identity] = merge_sha

    webhook_payload = {
        "object_kind": "merge_request",
        "project": {"path_with_namespace": repo_identity, "name": "hello-container"},
        "user_username": "reviewer-pat",
        "object_attributes": {
            "action": "merge",
            "id": 1002,
            "iid": 43,
            "target_branch": "main",
            "source_branch": "netci/pipeline-87654321",
            "merge_commit_sha": merge_sha,
            "last_commit": {"id": "b" * 40},
        },
    }
    client.post(
        "/webhooks/scm/gitlab",
        json=webhook_payload,
        headers={
            "X-Gitlab-Event": "Merge Request Hook",
            "X-Gitlab-Delivery": "delivery-merge-ci",
            "X-Gitlab-Token": token,
        },
    )

    # Launch pipeline run
    run_resp = client.post(
        "/modules/hello-container/pipeline-runs",
        json={"commitSha": "c" * 40, "branch": "main", "environment": "dev"},
    )
    assert run_resp.status_code == 202, run_resp.text

    assert len(captured) == 1
    launch_req = captured[0]
    assert "post-test" in launch_req.stages
    assert launch_req.custom_stages == [
        {
            "id": "post-test",
            "name": "Post Test",
            "script": ".netci/stages/post-test.sh",
            "after": "unit-test",
            "env": {},
        }
    ]


# -----------------------------------------------------------------------------
# Regressions found in review
# -----------------------------------------------------------------------------

MERGED_STAGES = [
    {"id": "checkout"},
    {"id": "unit-test"},
    {"id": "post-test", "name": "Post Test", "after": "unit-test"},
    {"id": "build"},
    {"id": "sbom"},
    {"id": "vulnerability-scan"},
    {"id": "sign"},
    {"id": "publish"},
]


def _merge_hook(client, token, delivery, *, merge_sha, target="main", repo="acme/hello-container"):
    return client.post(
        "/webhooks/scm/gitlab",
        json={
            "object_kind": "merge_request",
            "project": {"path_with_namespace": repo, "name": "hello-container"},
            "user_username": "reviewer-pat",
            "object_attributes": {
                "action": "merge", "id": 2000, "iid": 50, "target_branch": target,
                "source_branch": "netci/pipeline-0badc0de", "merge_commit_sha": merge_sha,
                "last_commit": {"id": "9" * 40},
            },
        },
        headers={"X-Gitlab-Event": "Merge Request Hook", "X-Gitlab-Delivery": delivery, "X-Gitlab-Token": token},
    )


def _audits(event_type):
    with main.database.transaction() as session:
        return [a for a in session.audit_records() if a.event_type == event_type]


def test_merge_keeps_the_module_deployment_targets(fake_gitlab):
    # The revision the merge writes is about stages. It once passed `deploymentConfig`,
    # a key the module JSON does not have, and so wrote a revision with no targets.
    client = TestClient(main.app)
    configure_gitlab_scm("hello-container", token="t0ken")
    before = main.portal.module("hello-container")["deploymentEnvironments"]
    assert before, "fixture module must have deployment targets for this test to mean anything"
    fake_gitlab.heads["acme/hello-container"] = "d" * 40
    fake_gitlab.files[("acme/hello-container", ".netci/pipeline.yaml", "d" * 40)] = render_pipeline_yaml(MERGED_STAGES)

    resp = _merge_hook(client, "t0ken", "keep-targets", merge_sha="d" * 40)

    assert resp.json()["status"] == "pipeline_applied", resp.text
    assert main.portal.module("hello-container")["deploymentEnvironments"] == before


@pytest.mark.parametrize("yaml_text, reason", [
    ("version: 1\nstages: [{id: checkout, script: /etc/passwd}]\n", "Unknown key"),
    ("version: 2\nstages: []\n", "Unsupported"),
    # Removing a required stage in the branch after netCI opened it.
    ("version: 1\nstages: [{id: checkout}, {id: unit-test}]\n", "Required stages"),
    # A custom stage anchored after a stage the pipeline does not run.
    ("version: 1\nstages: [{id: checkout}, {id: x-lint, after: nope}, {id: build}, {id: sbom},"
     " {id: vulnerability-scan}, {id: sign}, {id: publish}]\n", "anchor"),
])
def test_merge_of_a_bad_pipeline_is_rejected_and_audited(fake_gitlab, yaml_text, reason):
    client = TestClient(main.app)
    configure_gitlab_scm("hello-container", token="t0ken")
    stages_before = main.platform.get_application(
        UUID(str(main.portal.module("hello-container")["applicationId"]))).stages
    fake_gitlab.heads["acme/hello-container"] = "e" * 40
    fake_gitlab.files[("acme/hello-container", ".netci/pipeline.yaml", "e" * 40)] = yaml_text

    resp = _merge_hook(client, "t0ken", f"bad-{reason}", merge_sha="e" * 40)

    assert resp.status_code == 200
    assert resp.json()["status"] == "pipeline_rejected"
    assert reason in resp.json()["reason"]
    assert _audits("pipeline.merge_rejected")
    app_uuid = UUID(str(main.portal.module("hello-container")["applicationId"]))
    assert main.platform.get_application(app_uuid).stages == stages_before
    assert "customStages" not in main.portal.module("hello-container")["pipelineConfig"]


def test_merge_into_another_branch_changes_nothing(fake_gitlab):
    client = TestClient(main.app)
    configure_gitlab_scm("hello-container", token="t0ken")
    fake_gitlab.heads["acme/hello-container"] = "f" * 40
    fake_gitlab.files[("acme/hello-container", ".netci/pipeline.yaml", "f" * 40)] = render_pipeline_yaml(MERGED_STAGES)

    resp = _merge_hook(client, "t0ken", "other-branch", merge_sha="f" * 40, target="release-1")

    assert resp.json()["status"] == "pipeline_rejected"
    assert "not the default branch" in resp.json()["reason"]
    assert "customStages" not in main.portal.module("hello-container")["pipelineConfig"]


def test_gitlab_failure_reading_stage_code_is_502_not_an_empty_editor(fake_gitlab, monkeypatch):
    client = TestClient(main.app)
    configure_gitlab_scm("hello-container")
    main.platform.register_custom_stage(
        actor="admin", requires_approval=False, stage_id="flaky", name="Flaky",
        script=".netci/stages/flaky.sh", after_stage="unit-test",
    )

    def down(*_args, **_kwargs):
        raise GitLabRepoError(401, "GitLab API returned 401: token expired")

    monkeypatch.setattr(gitlab_repo, "get_file", down)
    resp = client.get("/modules/hello-container/pipeline/stages/flaky/code")

    # 502, not GitLab's 401: the caller is authorised, netCI's token is not.
    assert resp.status_code == 502
    assert resp.json()["code"] == "GITLAB_ERROR"


def test_builtin_stage_with_code_is_refused_not_stripped():
    stages = [{"id": "checkout"}, {"id": "unit-test", "code": "rm -rf /"}, {"id": "build"},
              {"id": "sbom"}, {"id": "vulnerability-scan"}, {"id": "sign"}, {"id": "publish"}]
    with pytest.raises(PipelineProposalError) as exc:
        validate_proposal(catalog_map(), TEMPLATE_STAGES, stages)
    assert exc.value.code == "PIPELINE_STAGE_ID_TAKEN"


@pytest.mark.parametrize("custom, error", [
    ({"id": "lint", "after": "unit-test", "script": "../../deploy.sh"}, "script must be .netci/stages/lint.sh"),
    ({"id": "lint", "after": "unit-test", "script": "/bin/sh"}, "script must be"),
    ({"id": "Lint", "after": "unit-test"}, "must match"),
    ({"id": "lint", "after": "not-a-stage"}, "not a stage of this template"),
    ({"id": "lint", "after": "unit-test", "env": {"A": "1"}}, "unknown field"),
])
def test_config_revision_cannot_carry_a_custom_stage_the_designer_would_refuse(custom, error):
    # customStages reaches the CI launch; a revision written by hand must meet the same rules.
    module = main.portal.module("hello-container")
    with pytest.raises(main.PortalError) as exc:
        main.portal.propose_config_revision(
            "hello-container",
            pipeline_config={**module["pipelineConfig"], "customStages": [custom]},
            deployment_config=module["deploymentEnvironments"],
            change_summary="hand edit",
            actor="someone",
        )
    assert exc.value.code == "INVALID_CUSTOM_STAGES"
    assert error in exc.value.message


# -----------------------------------------------------------------------------
# Second review (cross-model): ordering, races, approval, non-JSON
# -----------------------------------------------------------------------------

OLDER = [s for s in MERGED_STAGES if s["id"] != "post-test"]


def test_a_late_delivery_of_an_older_merge_does_not_revert_the_pipeline(fake_gitlab):
    # Merge A (no custom stage) then merge B (adds post-test); A's webhook arrives last.
    client = TestClient(main.app)
    configure_gitlab_scm("hello-container", token="t0ken")
    repo = "acme/hello-container"
    fake_gitlab.files[(repo, ".netci/pipeline.yaml", "a" * 40)] = render_pipeline_yaml(OLDER)
    fake_gitlab.files[(repo, ".netci/pipeline.yaml", "b" * 40)] = render_pipeline_yaml(MERGED_STAGES)
    fake_gitlab.heads[repo] = "b" * 40

    assert _merge_hook(client, "t0ken", "merge-b", merge_sha="b" * 40).json()["status"] == "pipeline_applied"
    late = _merge_hook(client, "t0ken", "merge-a", merge_sha="a" * 40).json()

    assert late["headCommit"] == "b" * 40
    app_uuid = UUID(str(main.portal.module("hello-container")["applicationId"]))
    assert "post-test" in main.platform.get_application(app_uuid).stages


def test_a_head_that_moves_while_applying_is_applied_again(fake_gitlab):
    client = TestClient(main.app)
    configure_gitlab_scm("hello-container", token="t0ken")
    repo = "acme/hello-container"
    fake_gitlab.files[(repo, ".netci/pipeline.yaml", "a" * 40)] = render_pipeline_yaml(OLDER)
    fake_gitlab.files[(repo, ".netci/pipeline.yaml", "b" * 40)] = render_pipeline_yaml(MERGED_STAGES)
    # read A, then the re-check sees B, then B is read and confirmed
    fake_gitlab.heads[repo] = ["a" * 40, "b" * 40]

    body = _merge_hook(client, "t0ken", "moving", merge_sha="a" * 40).json()

    assert body == {"status": "pipeline_applied", "stages": [s["id"] for s in MERGED_STAGES],
                    "headCommit": "b" * 40, "deliveryId": "moving"}


def test_losing_every_race_reports_superseded_not_applied(fake_gitlab, monkeypatch):
    client = TestClient(main.app)
    configure_gitlab_scm("hello-container", token="t0ken")
    repo = "acme/hello-container"
    fake_gitlab.files[(repo, ".netci/pipeline.yaml", "c" * 40)] = render_pipeline_yaml(MERGED_STAGES)
    fake_gitlab.heads[repo] = "c" * 40

    def always_conflict(*_args, **_kwargs):
        raise main.PortalError("CONCURRENT_MODIFICATION", "config_version moved", 409)

    monkeypatch.setattr(main.portal, "propose_config_revision", always_conflict)
    body = _merge_hook(client, "t0ken", "lost", merge_sha="c" * 40).json()

    assert body["status"] == "pipeline_superseded"
    app_uuid = UUID(str(main.portal.module("hello-container")["applicationId"]))
    assert "post-test" not in main.platform.get_application(app_uuid).stages


def test_the_merge_revision_never_needs_a_second_approver(fake_gitlab, monkeypatch):
    # A pending revision would leave the stage list behind; it must not be reachable.
    monkeypatch.setenv("NETCI_REQUIRE_CONFIG_APPROVAL", "true")
    client = TestClient(main.app)
    configure_gitlab_scm("hello-container", token="t0ken")
    envs = [str(e.get("environment")) for e in main.portal.module("hello-container")["deploymentEnvironments"]]
    assert "prod" in envs, "the fixture must have a production target for this to mean anything"
    fake_gitlab.files[("acme/hello-container", ".netci/pipeline.yaml", "9" * 40)] = render_pipeline_yaml(MERGED_STAGES)
    fake_gitlab.heads["acme/hello-container"] = "9" * 40

    body = _merge_hook(client, "t0ken", "approval", merge_sha="9" * 40).json()

    assert body["status"] == "pipeline_applied"


def test_a_200_that_is_not_json_is_a_gitlab_error(monkeypatch):
    class Html:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def read(self):
            return b"<html>proxy error</html>"

    monkeypatch.setenv("NETCI_GITLAB_TOKEN", "x")
    monkeypatch.setattr(gitlab_repo.urllib.request, "urlopen", lambda *_a, **_k: Html())
    client = gitlab_repo.GitLabRepoClient()
    client._gitlab_token = "x"
    with pytest.raises(GitLabRepoError) as exc:
        client.default_branch("acme/app")
    assert exc.value.status == 502
