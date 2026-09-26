"""HTTP routes and enforcement tests for change freezes (ADR-047)."""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

import app.main as main_mod
from app.adapters.scm import MockScmProvider, get_scm_provider, set_scm_provider
from app.domain.models import ScmProviderType
from app.store.records import BreakGlassRecord, ChangeFreezeRecord
from toolchain_report import declared_tool_report

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
                "signature": {"provider": "cosign", "verified": True, "certificateIdentity": "netci"}, "toolVersions": declared_tool_report(),
            })
            assert published.status_code == 202, published.text
    return client.post(f"/pipeline-runs/{run_id}/ci-result", headers=MACHINE, json=body)


def _healthy(deployment_id):
    response = client.post(f"/deployments/{deployment_id}/result", headers=MACHINE,
                           json={"status": "healthy", "message": "health check passed"})
    assert response.status_code == 202, response.text


def test_create_list_and_cancel_change_freeze():
    now = datetime.now(timezone.utc)
    starts_at = now - timedelta(hours=1)
    ends_at = now + timedelta(days=2)
    res = client.post(
        "/change-freezes",
        json={
            "name": "freeze-dev-maint",
            "startsAt": starts_at.isoformat(),
            "endsAt": ends_at.isoformat(),
            "environments": ["dev"],
            "reason": "maintenance window",
        },
    )
    assert res.status_code == 201, res.text
    data = res.json()
    assert data["name"] == "freeze-dev-maint"
    assert data["createdBy"] == "anonymous"
    assert data["environments"] == ["dev"]
    assert data["reason"] == "maintenance window"
    assert data["cancelledAt"] is None
    assert data["cancelledBy"] is None
    freeze_id = data["id"]

    list_res = client.get("/change-freezes")
    assert list_res.status_code == 200
    items = list_res.json()["items"]
    assert any(item["id"] == freeze_id for item in items)

    cancel_res = client.post(f"/change-freezes/{freeze_id}/cancel")
    assert cancel_res.status_code == 200, cancel_res.text
    cancelled_data = cancel_res.json()
    assert cancelled_data["id"] == freeze_id
    assert cancelled_data["cancelledAt"] is not None
    assert cancelled_data["cancelledBy"] == "anonymous"

    list_after = client.get("/change-freezes").json()["items"]
    assert not any(item["id"] == freeze_id for item in list_after)


def test_cancel_change_freeze_twice_returns_409():
    now = datetime.now(timezone.utc)
    res = client.post(
        "/change-freezes",
        json={
            "name": "freeze-twice",
            "startsAt": (now - timedelta(hours=1)).isoformat(),
            "endsAt": (now + timedelta(days=1)).isoformat(),
            "environments": ["dev"],
            "reason": "cancel test",
        },
    )
    assert res.status_code == 201
    freeze_id = res.json()["id"]

    first_cancel = client.post(f"/change-freezes/{freeze_id}/cancel")
    assert first_cancel.status_code == 200

    second_cancel = client.post(f"/change-freezes/{freeze_id}/cancel")
    assert second_cancel.status_code == 409
    code = second_cancel.json().get("code") or second_cancel.json().get("detail", {}).get("code")
    assert code == "FREEZE_NOT_ACTIVE"


def test_change_freeze_validation_ends_at_before_starts_at():
    now = datetime.now(timezone.utc)
    res = client.post(
        "/change-freezes",
        json={
            "name": "invalid-dates",
            "startsAt": (now + timedelta(days=2)).isoformat(),
            "endsAt": (now + timedelta(days=1)).isoformat(),
            "environments": ["dev"],
            "reason": "test invalid date order",
        },
    )
    assert res.status_code == 422

    res_equal = client.post(
        "/change-freezes",
        json={
            "name": "invalid-dates-equal",
            "startsAt": now.isoformat(),
            "endsAt": now.isoformat(),
            "environments": ["dev"],
            "reason": "test equal dates",
        },
    )
    assert res_equal.status_code == 422


def test_change_freeze_validation_over_31_days():
    now = datetime.now(timezone.utc)
    res = client.post(
        "/change-freezes",
        json={
            "name": "too-long-freeze",
            "startsAt": now.isoformat(),
            "endsAt": (now + timedelta(days=32)).isoformat(),
            "environments": ["dev"],
            "reason": "test over 31 days",
        },
    )
    assert res.status_code == 422


def test_change_freeze_unknown_module_id_returns_404():
    now = datetime.now(timezone.utc)
    res = client.post(
        "/change-freezes",
        json={
            "name": "mod-freeze",
            "startsAt": now.isoformat(),
            "endsAt": (now + timedelta(days=1)).isoformat(),
            "environments": ["dev"],
            "reason": "module specific",
            "moduleId": "non-existent-module-xyz",
        },
    )
    assert res.status_code == 404
    code = res.json().get("code") or res.json().get("detail", {}).get("code")
    assert code == "MODULE_NOT_FOUND"


def test_list_change_freezes_include_past_filter():
    now = datetime.now(timezone.utc)
    past_record = ChangeFreezeRecord(
        id=uuid4(),
        name="ended-freeze",
        starts_at=now - timedelta(days=10),
        ends_at=now - timedelta(days=2),
        environments=("dev",),
        reason="past freeze",
        created_by="anonymous",
    )
    with main_mod.database.transaction() as tx:
        tx.insert_change_freeze(past_record)

    default_list = client.get("/change-freezes").json()["items"]
    assert not any(i["id"] == str(past_record.id) for i in default_list)

    past_list = client.get("/change-freezes", params={"includePast": "true"}).json()["items"]
    assert any(i["id"] == str(past_record.id) for i in past_list)


def test_push_to_main_during_active_dev_freeze_builds_without_deploying():
    module, repo = _module()
    now = datetime.now(timezone.utc)
    freeze_res = client.post(
        "/change-freezes",
        json={
            "name": "winter-lockdown",
            "startsAt": (now - timedelta(hours=1)).isoformat(),
            "endsAt": (now + timedelta(days=1)).isoformat(),
            "environments": ["dev"],
            "reason": "winter freeze",
        },
    )
    assert freeze_res.status_code == 201

    hook = _push(repo, "refs/heads/main")
    assert hook.status_code == 201, hook.text
    run_id = hook.json()["pipelineRunId"]

    done = _succeed(run_id)
    assert done.status_code == 202, done.text
    body = done.json()
    assert body["deployment"] is None
    assert body["pipelineRun"]["status"] == "succeeded"

    logs = client.get(f"/pipeline-runs/{run_id}/logs").json()
    all_logs = " ".join(logs["lines"])
    assert "not deployed" in all_logs
    assert "winter-lockdown" in all_logs


def test_promotion_to_dev_during_freeze_refused_and_allowed_after_cancel():
    module, repo = _module()
    run_id = _push(repo, "refs/heads/feature/promote").json()["pipelineRunId"]
    _succeed(run_id)

    now = datetime.now(timezone.utc)
    freeze_res = client.post(
        "/change-freezes",
        json={
            "name": "dev-freeze",
            "startsAt": (now - timedelta(hours=1)).isoformat(),
            "endsAt": (now + timedelta(days=1)).isoformat(),
            "environments": ["dev"],
            "reason": "dev is frozen",
        },
    )
    assert freeze_res.status_code == 201
    freeze_id = freeze_res.json()["id"]

    blocked = client.post(f"/modules/{module['id']}/promotions", json={"pipelineRunId": run_id, "environment": "dev"})
    assert blocked.status_code == 409, blocked.text
    code = blocked.json().get("code") or blocked.json().get("detail", {}).get("code")
    assert code == "CHANGE_FREEZE"

    cancel_res = client.post(f"/change-freezes/{freeze_id}/cancel")
    assert cancel_res.status_code == 200

    allowed = client.post(f"/modules/{module['id']}/promotions", json={"pipelineRunId": run_id, "environment": "dev"})
    assert allowed.status_code == 202, allowed.text


def test_freeze_scoping_rules():
    module1, repo1 = _module("mod-alpha")
    module2, repo2 = _module("mod-beta")

    run_id1 = _push(repo1, "refs/heads/feature/a").json()["pipelineRunId"]
    _succeed(run_id1)

    now = datetime.now(timezone.utc)
    # Freeze scoped to module2 does not block module1
    client.post(
        "/change-freezes",
        json={
            "name": "freeze-mod2-only",
            "startsAt": (now - timedelta(hours=1)).isoformat(),
            "endsAt": (now + timedelta(days=1)).isoformat(),
            "environments": ["dev"],
            "reason": "beta only freeze",
            "moduleId": module2["id"],
        },
    )
    p1 = client.post(f"/modules/{module1['id']}/promotions", json={"pipelineRunId": run_id1, "environment": "dev"})
    assert p1.status_code == 202, p1.text
    # Let it finish: the next promotion to the same target would otherwise, correctly,
    # meet the lease this one holds (DEPLOYMENT_TARGET_BUSY), not the freeze under test.
    _healthy(p1.json()["deploymentId"])

    # Staging-only freeze does not block dev
    client.post(
        "/change-freezes",
        json={
            "name": "staging-freeze",
            "startsAt": (now - timedelta(hours=1)).isoformat(),
            "endsAt": (now + timedelta(days=1)).isoformat(),
            "environments": ["staging"],
            "reason": "staging only freeze",
        },
    )
    run_id1_b = _push(repo1, "refs/heads/feature/b").json()["pipelineRunId"]
    _succeed(run_id1_b)
    p2 = client.post(f"/modules/{module1['id']}/promotions", json={"pipelineRunId": run_id1_b, "environment": "dev"})
    assert p2.status_code == 202, p2.text
    _healthy(p2.json()["deploymentId"])

    # Freeze with the module's systemId blocks dev
    client.post(
        "/change-freezes",
        json={
            "name": "system-freeze",
            "startsAt": (now - timedelta(hours=1)).isoformat(),
            "endsAt": (now + timedelta(days=1)).isoformat(),
            "environments": ["dev"],
            "reason": "entire system freeze",
            "systemId": module1["systemId"],
        },
    )
    run_id1_c = _push(repo1, "refs/heads/feature/c").json()["pipelineRunId"]
    _succeed(run_id1_c)
    p3 = client.post(f"/modules/{module1['id']}/promotions", json={"pipelineRunId": run_id1_c, "environment": "dev"})
    assert p3.status_code == 409
    code = p3.json().get("code") or p3.json().get("detail", {}).get("code")
    assert code == "CHANGE_FREEZE"


def test_break_glass_bypasses_active_change_freeze():
    module, repo = _module()
    run_id = _push(repo, "refs/heads/feature/bg-test").json()["pipelineRunId"]
    _succeed(run_id)

    now = datetime.now(timezone.utc)
    freeze_res = client.post(
        "/change-freezes",
        json={
            "name": "dev-freeze-for-bg",
            "startsAt": (now - timedelta(hours=1)).isoformat(),
            "endsAt": (now + timedelta(days=1)).isoformat(),
            "environments": ["dev"],
            "reason": "emergency lockdown",
        },
    )
    freeze_id = freeze_res.json()["id"]

    blocked = client.post(f"/modules/{module['id']}/promotions", json={"pipelineRunId": run_id, "environment": "dev"})
    assert blocked.status_code == 409
    code = blocked.json().get("code") or blocked.json().get("detail", {}).get("code")
    assert code == "CHANGE_FREEZE"

    bg_record = BreakGlassRecord(
        id=uuid4(),
        target_type="change_freeze",
        target_id=str(freeze_id),
        requested_by="dev1",
        reason="emergency fix for P0",
        incident_ticket="INC-4321",
        status="active",
        created_at=now,
        approved_by="approver1",
        approved_at=now,
        expires_at=now + timedelta(hours=1),
    )
    with main_mod.database.transaction() as tx:
        tx.insert_break_glass_request(bg_record)

    allowed = client.post(f"/modules/{module['id']}/promotions", json={"pipelineRunId": run_id, "environment": "dev"})
    assert allowed.status_code == 202, allowed.text


def test_rollback_is_not_blocked_by_change_freeze():
    module, repo = _module()
    hook = _push(repo, "refs/heads/main")
    run_id = hook.json()["pipelineRunId"]
    done = _succeed(run_id)
    deployment_id = done.json()["deployment"]["id"]
    _healthy(deployment_id)

    now = datetime.now(timezone.utc)
    freeze_res = client.post(
        "/change-freezes",
        json={
            "name": "dev-freeze-for-rollback",
            "startsAt": (now - timedelta(hours=1)).isoformat(),
            "endsAt": (now + timedelta(days=1)).isoformat(),
            "environments": ["dev"],
            "reason": "lockdown dev",
        },
    )
    assert freeze_res.status_code == 201

    rollback_res = client.post(
        f"/deployments/{deployment_id}/rollback",
        json={"targetArtifactDigest": "sha256:" + "d" * 64, "reason": "restore"},
    )
    assert rollback_res.status_code in (200, 201, 202), rollback_res.text
    assert rollback_res.status_code != 409


def test_production_request_scheduled_inside_prod_freeze_fails():
    module, repo = _module()
    push_res = _push(repo, "refs/tags/v3.0.0")
    run_id = push_res.json()["pipelineRunId"]
    _succeed(run_id)

    now = datetime.now(timezone.utc)
    freeze_start = now + timedelta(days=2)
    freeze_end = now + timedelta(days=4)
    client.post(
        "/change-freezes",
        json={
            "name": "prod-blackout",
            "startsAt": freeze_start.isoformat(),
            "endsAt": freeze_end.isoformat(),
            "environments": ["prod"],
            "reason": "blackout window",
        },
    )

    scheduled_inside = freeze_start + timedelta(hours=6)
    req_res = client.post(
        "/production-requests",
        json={
            "modules": [{"moduleId": module["id"], "version": "v3.0.0"}],
            "scheduledFor": scheduled_inside.isoformat(),
        },
    )
    assert req_res.status_code == 409, req_res.text
    code = req_res.json().get("code") or req_res.json().get("detail", {}).get("code")
    assert code == "CHANGE_FREEZE"


def test_approving_a_production_deployment_during_a_prod_freeze_is_refused():
    application = client.post("/applications", json={
        "name": "frozen-prod", "repositoryUrl": "https://git.example/frozen-prod",
        "pipelineTemplate": "container-ci-cd-v1", "runtime": "docker",
    }).json()
    run = client.post(f"/applications/{application['id']}/pipeline-runs",
                      json={"commitSha": "abcdef1234567", "environment": "prod"}).json()
    client.post(f"/pipeline-runs/{run['id']}/ci-result", headers=MACHINE, json={"status": "running"})
    pending = client.post(f"/pipeline-runs/{run['id']}/ci-result", headers=MACHINE,
                          json={"status": "succeeded", "artifactDigest": "sha256:" + "b" * 64}).json()["deployment"]
    assert pending["status"] == "pending_approval"

    now = datetime.now(timezone.utc)
    freeze = client.post("/change-freezes", json={
        "name": "sale weekend", "startsAt": (now - timedelta(minutes=5)).isoformat(),
        "endsAt": (now + timedelta(hours=2)).isoformat(), "environments": ["prod"], "reason": "sale",
    })
    assert freeze.status_code == 201, freeze.text

    approved = client.post(f"/deployments/{pending['id']}/approve", json={"comment": "go"})
    assert approved.status_code == 409 and approved.json()["code"] == "CHANGE_FREEZE"
    assert client.get(f"/deployments/{pending['id']}").json()["status"] == "pending_approval"
