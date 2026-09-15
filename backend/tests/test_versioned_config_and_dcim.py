"""Phase 8 (P1.3): Versioned environment configuration and DCIM lifecycle tests.

Verifies:
1. Immutable configuration revision history (module_config_revisions).
2. Active revision pointer with compare-and-set optimistic locking (config_version).
3. Pipeline runs and deployments pin the active config_revision_id at execution time.
4. Non-prod configuration changes auto-activate; prod configuration changes require approval.
5. Separation of duties: author proposing production change cannot approve it (403 SEPARATION_OF_DUTIES).
6. Config revision diff viewer and 1-click rollback creating a forward immutable revision.
7. Fail-closed DCIM target revalidation rejecting decommissioned or offline hosts (422 DCIM_TARGET_UNAVAILABLE).
8. DCIM server health observation and freshness tracking.
9. Drift detection comparing active desired revision against running deployments and DCIM status.
"""

from __future__ import annotations

import hashlib
import os
import secrets
from pathlib import Path
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
import yaml

import app.main as main
from app.adapters.dcim import (
    TargetValidationResult,
    UnconfiguredDcimCatalog,
)
from app.domain.models import ServerHealthRecord
from app.portal import PortalError


def _tokens_file(tmp_path: Path, principals: list[dict]) -> tuple[Path, dict[str, str]]:
    tokens: dict[str, str] = {}
    entries = []
    for principal in principals:
        token = f"tok-{principal['subject']}-{secrets.token_urlsafe(8)}"
        tokens[principal["subject"]] = token
        entries.append({**principal, "tokenSha256": hashlib.sha256(token.encode()).hexdigest()})
    path = tmp_path / "tokens.yaml"
    path.write_text(yaml.safe_dump({"principals": entries}), encoding="utf-8")
    return path, tokens


@pytest.fixture
def auth_client(tmp_path, monkeypatch):
    path, tokens = _tokens_file(
        tmp_path,
        [
            {"subject": "dana", "displayName": "Dana Developer", "roles": ["developer"]},
            {"subject": "raj", "displayName": "Raj Reviewer", "roles": ["developer", "reviewer"]},
            {"subject": "pat", "displayName": "Pat Platform", "roles": ["platform-admin"]},
        ],
    )
    monkeypatch.setenv("NETCI_AUTH_MODE", "token")
    monkeypatch.setenv("NETCI_AUTH_TOKENS_FILE", str(path))
    import app.auth as auth_mod
    monkeypatch.setattr(main, "authenticator", auth_mod.build_authenticator())

    client = TestClient(main.app)

    def headers_for(user: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {tokens[user]}"}

    return client, headers_for


def setup_function():
    main.platform.reset()
    main.portal.reset()


def test_initial_module_has_active_revision_and_history(auth_client):
    client, headers_for = auth_client
    res = client.get("/modules/hello-container/config-revisions", headers=headers_for("dana"))
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["moduleId"] == "hello-container"
    assert body["configVersion"] >= 1
    assert body["activeRevisionId"] is not None
    assert len(body["items"]) >= 1
    rev1 = body["items"][0]
    assert rev1["revisionNumber"] == 1
    assert rev1["status"] == "active"
    assert rev1["active"] is True


def test_non_prod_config_change_auto_activates(auth_client):
    client, headers_for = auth_client
    rev1 = client.get("/modules/hello-container/config-revisions", headers=headers_for("dana")).json()["items"][0]
    prod_targets = [c for c in rev1["deploymentConfig"] if c.get("environment") == "prod"]

    res = client.post(
        "/modules/hello-container/config-revisions",
        headers=headers_for("dana"),
        json={
            "changeSummary": "Update dev test stage",
            "pipelineConfig": {
                "runner": "jenkins-dev",
                "stages": ["checkout", "test", "build"],
            },
            "deploymentConfig": [
                {"environment": "dev", "servers": ["srv-dev-01.internal"]},
                *prod_targets,
            ],
        },
    )
    assert res.status_code == 201, res.text
    data = res.json()
    assert data["revisionNumber"] == 2
    assert data["status"] == "active"
    assert data["active"] is True
    assert data["requiresApproval"] is False
    assert data["createdBy"] == "dana"

    # Verify history now has 2 revisions
    list_res = client.get("/modules/hello-container/config-revisions", headers=headers_for("dana"))
    revisions = list_res.json()["items"]
    assert len(revisions) == 2
    r2 = [r for r in revisions if r["revisionNumber"] == 2][0]
    r1 = [r for r in revisions if r["revisionNumber"] == 1][0]
    assert r2["active"] is True
    assert r1["active"] is False  # previous is superseded


def test_production_config_change_requires_approval_and_enforces_separation_of_duties(auth_client):
    client, headers_for = auth_client
    # Dana proposes a change affecting prod environment
    res = client.post(
        "/modules/hello-container/config-revisions",
        headers=headers_for("dana"),
        json={
            "changeSummary": "Add new prod server",
            "deploymentConfig": [
                {"environment": "dev", "servers": ["srv-dev-01.internal"]},
                {"environment": "prod", "servers": ["srv-prod-01.internal", "srv-prod-02.internal"]},
            ],
        },
    )
    assert res.status_code == 201, res.text
    data = res.json()
    assert data["revisionNumber"] == 2
    assert data["status"] == "pending_approval"
    assert data["active"] is False
    assert data["requiresApproval"] is True
    assert data["createdBy"] == "dana"
    rev_id = data["id"]

    # Active revision should still be rev 1
    list_res = client.get("/modules/hello-container/config-revisions", headers=headers_for("dana"))
    items = list_res.json()["items"]
    rev1 = [r for r in items if r["revisionNumber"] == 1][0]
    rev2 = [r for r in items if r["revisionNumber"] == 2][0]
    assert rev1["active"] is True
    assert rev2["active"] is False

    # Dana tries to approve her own change -> 403 SEPARATION_OF_DUTIES
    self_approve = client.post(
        f"/modules/hello-container/config-revisions/{rev_id}/approve",
        headers=headers_for("dana"),
    )
    # Dana is developer role, which doesn't have ReviewerAccess; but even if she had, separation of duties applies.
    assert self_approve.status_code in (403, 401)

    # Now let Raj (a reviewer) propose a prod change on hello-kubernetes and try to self-approve
    raj_prop = client.post(
        "/modules/hello-kubernetes/config-revisions",
        headers=headers_for("raj"),
        json={
            "changeSummary": "Prod scale out",
            "deploymentConfig": [{"environment": "prod", "servers": ["srv-prod-k8s-1", "srv-prod-k8s-2"]}],
        },
    )
    assert raj_prop.status_code == 201
    raj_rev_id = raj_prop.json()["id"]

    # Raj has ReviewerAccess, but is the creator -> MUST FAIL WITH 403 SEPARATION_OF_DUTIES
    raj_self_approve = client.post(
        f"/modules/hello-kubernetes/config-revisions/{raj_rev_id}/approve",
        headers=headers_for("raj"),
    )
    assert raj_self_approve.status_code == 403
    assert raj_self_approve.json()["detail"]["code"] == "SEPARATION_OF_DUTIES"

    # A different reviewer (Pat - platform-admin) approves Raj's revision -> SUCCESS
    pat_approve = client.post(
        f"/modules/hello-kubernetes/config-revisions/{raj_rev_id}/approve",
        headers=headers_for("pat"),
    )
    assert pat_approve.status_code == 200
    assert pat_approve.json()["status"] == "active"
    assert pat_approve.json()["active"] is True
    assert pat_approve.json()["approvedBy"] == "pat"


def test_reject_config_revision(auth_client):
    client, headers_for = auth_client
    # Dana proposes a prod change
    res = client.post(
        "/modules/hello-container/config-revisions",
        headers=headers_for("dana"),
        json={
            "changeSummary": "Risky prod change",
            "deploymentConfig": [{"environment": "prod", "servers": ["srv-untested-01"]}],
        },
    )
    assert res.status_code == 201
    rev_id = res.json()["id"]

    # Raj rejects it with reason
    rej = client.post(
        f"/modules/hello-container/config-revisions/{rev_id}/reject",
        headers=headers_for("raj"),
        json={"reason": "Target host not in compliance zone"},
    )
    assert rej.status_code == 200
    data = rej.json()
    assert data["status"] == "rejected"
    assert data["active"] is False
    assert data["rejectionReason"] == "Target host not in compliance zone"


def test_config_revision_diff(auth_client):
    client, headers_for = auth_client
    client.post(
        "/modules/hello-container/config-revisions",
        headers=headers_for("dana"),
        json={
            "changeSummary": "Rev 2 non-prod update",
            "pipelineConfig": {"runner": "jenkins-fast"},
            "deploymentConfig": [{"environment": "dev", "servers": ["srv-1"]}],
        },
    )

    diff_res = client.get(
        "/modules/hello-container/config-revisions/diff?fromRev=1&toRev=2",
        headers=headers_for("dana"),
    )
    assert diff_res.status_code == 200, diff_res.text
    diff = diff_res.json()
    assert diff["moduleId"] == "hello-container"
    assert diff["from"] == 1
    assert diff["to"] == 2
    assert diff["changeCount"] > 0
    paths = [c["path"] for c in diff["changes"]]
    assert any("runner" in p or "pipelineConfig" in p or "deploymentConfig" in p for p in paths)


def test_rollback_creates_new_revision_copying_past_target(auth_client):
    client, headers_for = auth_client
    # Rev 2
    client.post(
        "/modules/hello-container/config-revisions",
        headers=headers_for("dana"),
        json={
            "changeSummary": "Rev 2 changes",
            "pipelineConfig": {"runner": "jenkins-experimental"},
            "deploymentConfig": [{"environment": "dev", "servers": ["srv-dev-temp"]}],
        },
    )
    # Rollback to Rev 1
    rb_res = client.post(
        "/modules/hello-container/config-revisions/1/rollback",
        headers=headers_for("dana"),
    )
    assert rb_res.status_code == 200, rb_res.text
    data = rb_res.json()
    assert data["revisionNumber"] == 3
    assert data["rolledBackTo"] == 1
    assert "rollback to revision 1" in data["changeSummary"].lower()
    assert data["active"] is True


def test_pipeline_run_and_deployment_pins_config_revision(auth_client):
    client, headers_for = auth_client
    # Check active revision id for hello-container
    revs = client.get("/modules/hello-container/config-revisions", headers=headers_for("dana")).json()
    active_rev_id = revs["activeRevisionId"]
    assert active_rev_id is not None

    # Start a pipeline run
    run_res = client.post(
        "/modules/hello-container/pipeline-runs",
        headers=headers_for("dana"),
        json={
            "commitSha": "a" * 40,
            "branch": "main",
            "environment": "dev",
        },
    )
    assert run_res.status_code == 202, run_res.text
    run_data = run_res.json()
    assert run_data.get("configRevisionId") == active_rev_id

    # Create rev 2
    client.post(
        "/modules/hello-container/config-revisions",
        headers=headers_for("dana"),
        json={
            "changeSummary": "Rev 2",
            "pipelineConfig": {"runner": "jenkins-v2"},
            "deploymentConfig": [{"environment": "dev", "servers": ["srv-v2"]}],
        },
    )

    # Old run STILL has the pinned configRevisionId from launch
    old_run = client.get(f"/pipeline-runs/{run_data['id']}", headers=headers_for("dana")).json()
    assert old_run["configRevisionId"] == active_rev_id


def test_dcim_target_revalidation_fails_closed_on_decommissioned_server(monkeypatch):
    """If a deployment target host is decommissioned, deployment fails closed with 422."""
    class MockDecommissionedDcim(UnconfiguredDcimCatalog):
        def validate_target(self, system_id, module_id, environment, target):
            if "decom" in target or "localhost" in target:
                return TargetValidationResult(
                    valid=False,
                    message=f"Target {target} has been decommissioned in DCIM",
                    status="decommissioned",
                )
            return TargetValidationResult(valid=True, status="online", message="ok")

    dcim = MockDecommissionedDcim()
    monkeypatch.setattr(main.portal, "dcim_catalog", dcim)

    # Calling delivery_parameters should fail closed with DCIM_TARGET_UNAVAILABLE
    from app.domain.models import Environment
    with pytest.raises(PortalError) as excinfo:
        main.portal.delivery_parameters("hello-container", Environment.DEV)
    assert excinfo.value.code == "DCIM_TARGET_UNAVAILABLE"
    assert "decommissioned" in excinfo.value.message


def test_server_health_query(auth_client):
    client, headers_for = auth_client
    # Record health in store
    rec = ServerHealthRecord(
        server_name="srv-dcim-test-01.internal",
        status="online",
        source="dcim-agent",
        freshness_seconds=12,
        details={"load": 0.42},
    )
    with main.portal.database.transaction() as session:
        session.record_server_health(rec)

    res = client.get("/servers/health", headers=headers_for("dana"))
    assert res.status_code == 200
    data = res.json()
    assert data["count"] >= 1
    found = [s for s in data["items"] if s["serverName"] == "srv-dcim-test-01.internal"]
    assert len(found) == 1
    assert found[0]["status"] == "online"
    assert found[0]["source"] == "dcim-agent"


def test_drift_detection(auth_client):
    client, headers_for = auth_client
    res = client.get("/modules/hello-container/drift", headers=headers_for("dana"))
    assert res.status_code == 200
    data = res.json()
    assert data["moduleId"] == "hello-container"
    assert "hasDrift" in data
    assert "deploymentDrift" in data
    assert "dcimDrift" in data


def test_cas_concurrency_conflict():
    """Conflicting concurrent activation on an outdated config_version must fail with CONCURRENT_MODIFICATION."""
    with main.portal.database.transaction() as session:
        mod = session.portal_module("hello-container")
        assert mod is not None
        # Intentionally provide a stale expected config_version
        stale_version = mod.config_version - 1
        rev = session.active_config_revision("hello-container")
        assert rev is not None
        ok = session.set_module_active_revision("hello-container", rev.id, stale_version)
        assert ok is False, "CAS update with stale version must fail"


def test_unconfigured_dcim_never_fakes_online():
    """An unconfigured or probing DCIM adapter must never report fake 'online' status."""
    catalog = UnconfiguredDcimCatalog()
    record = catalog.probe_server_health("srv-unknown-host-01.internal")
    assert record.status in ("unknown", "not_configured")
    assert record.status != "online"
    assert record.source in ("unconfigured", "dcim-unconfigured")



# ------------------------------------------------ runtime settings are reviewed config


def test_runtime_settings_in_a_revision_become_playbook_inputs(auth_client):
    """The live lab failed at `/opt/netci-docker-demo: Permission denied` because the
    playbook defaults were the only way to say where a service lives. That belongs in
    the reviewed revision, and it must arrive at the playbook under the names the
    playbook reads."""
    client, headers_for = auth_client
    current = client.get("/modules/hello-container/config-revisions", headers=headers_for("dana")).json()
    active = next(r for r in current["items"] if r["active"])
    # Keep the production target as it is: dropping it would (rightly) need an approver.
    untouched = [c for c in active["deploymentConfig"] if c.get("environment") != "dev"]
    res = client.post(
        "/modules/hello-container/config-revisions",
        headers=headers_for("dana"),
        json={
            "changeSummary": "lab layout",
            "deploymentConfig": [
                {
                    "environment": "dev",
                    "servers": ["netci-local-docker-dev"],
                    "runtimeSettings": {
                        "appRoot": "/var/tmp/netci-lab/hello-container",
                        "hostPort": 18081,
                        "networkMode": "host",
                        "become": False,
                        "imagePullHost": "localhost:55000",
                    },
                },
                *untouched,
            ],
        },
    )
    assert res.status_code == 201, res.text
    assert res.json()["status"] == "active", res.text
    from app.domain.models import Environment

    parameters = main.portal.delivery_parameters("hello-container", Environment.DEV)
    assert parameters["app_root"] == "/var/tmp/netci-lab/hello-container"
    assert parameters["host_port"] == 18081
    assert parameters["network_mode"] == "host"
    assert parameters["netci_become"] is False
    assert parameters["image_pull_host"] == "localhost:55000"
    assert parameters["target_hosts"] == ["netci-local-docker-dev"]
    # Nothing else from the target leaks into the playbook namespace.
    assert "runtimeSettings" not in parameters


@pytest.mark.parametrize(
    "settings",
    [
        {"appRoot": "relative/path"},
        {"appRoot": "/opt/../etc"},
        {"appRoot": "/opt/x; rm -rf /"},
        {"hostPort": 80},
        {"networkMode": "none"},
        {"systemdScope": "root"},
        {"shell": "bash"},
        {"imagePullHost": "localhost:55000/evil"},
        {"imagePullHost": "http://localhost:55000"},
    ],
)
def test_runtime_settings_outside_the_playbook_contract_are_refused(auth_client, settings):
    client, headers_for = auth_client
    res = client.post(
        "/modules/hello-container/config-revisions",
        headers=headers_for("dana"),
        json={"deploymentConfig": [{"environment": "dev", "servers": ["h1"], "runtimeSettings": settings}]},
    )
    assert res.status_code == 422, res.text


@pytest.mark.parametrize(
    "target",
    [
        {"environment": "dev", "servers": ["host with space"]},
        {"environment": "dev", "servers": ["-leading-dash"]},
        {"environment": "dev", "namespace": "Not_Valid"},
        {"environment": "moon", "servers": ["h1"]},
        {"servers": ["h1"]},
    ],
)
def test_a_revision_target_is_validated_like_module_creation(auth_client, target):
    """A revision becomes the module's active target set; an unvalidated one could
    name a host the adapter would then pass to `--limit`."""
    client, headers_for = auth_client
    res = client.post(
        "/modules/hello-container/config-revisions",
        headers=headers_for("dana"),
        json={"deploymentConfig": [target]},
    )
    assert res.status_code == 422, res.text


def test_playbook_layout_cannot_be_supplied_per_run(auth_client):
    """The same names are refused as build inputs: a run may not move the install root."""
    client, headers_for = auth_client
    res = client.post(
        "/modules/hello-container/pipeline-runs",
        headers=headers_for("dana"),
        json={
            "commitSha": "a" * 40,
            "environment": "dev",
            "parameters": {"app_root": "/etc", "network_mode": "host"},
        },
    )
    assert res.status_code == 422, res.text
    assert res.json()["code"] == "BUILD_INPUT_NOT_ALLOWED"


def test_resubmitting_the_production_target_as_read_back_is_not_a_production_change(auth_client):
    """The API returns `null` for unset fields; a revision built from that read-back
    must not need an approver when production is byte-for-byte the same intent."""
    client, headers_for = auth_client
    current = client.get("/modules/hello-container/config-revisions", headers=headers_for("dana")).json()
    active = next(r for r in current["items"] if r["active"])
    res = client.post(
        "/modules/hello-container/config-revisions",
        headers=headers_for("dana"),
        json={"changeSummary": "no-op resubmit", "deploymentConfig": active["deploymentConfig"]},
    )
    assert res.status_code == 201, res.text
    assert res.json()["requiresApproval"] is False, res.text


def test_kubernetes_targets_carry_no_ansible_hosts(auth_client):
    """DCIM knows the cluster nodes; they are not `--limit` hosts. With them, the
    `hosts: localhost` play selected nothing and the deployment did nothing, quietly."""
    client, headers_for = auth_client
    from app.domain.models import Environment

    system = client.post("/systems", headers=headers_for("pat"), json={
        "id": "hello-kubernetes-sys", "unit": "lab", "description": "kubernetes sample",
    })
    assert system.status_code == 201, system.text
    created = client.post("/systems/hello-kubernetes-sys/modules", headers=headers_for("pat"), json={
        "name": "hello-k8s-target-test",
        "repositoryUrl": "https://github.com/example/hello-k8s-target-test",
        "pipelineTemplate": "kubernetes-ci-cd-v1",
        "runtime": "kubernetes",
        "deploymentEnvironments": [{
            "displayName": "Development", "environment": "dev", "runtime": "kubernetes",
            "servers": [], "tasks": [], "kubeconfigRef": "netci-dev-kubeconfig", "namespace": "dev",
        }],
    })
    assert created.status_code == 201, created.text
    parameters = main.portal.delivery_parameters("hello-k8s-target-test", Environment.DEV)
    assert parameters["target_hosts"] == []
    assert parameters["target_namespace"] == "dev"
    assert parameters["kubeconfig_ref"] == "netci-dev-kubeconfig"
