"""What a company's Jenkins exposes that the lab did not: long builds, runs waiting behind
netCI's own queue, pod templates a caller should not pick, tokens from other builds, and a
missing setting that would silently fall back to the lab's."""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

import app.main as main_mod
from app import workload_identity
from app.build_inputs import BuildInputError, validate_build_inputs
from app.delivery import DeliveryPlatform
from app.domain.models import Environment, PipelineStatus, Runtime, utc_now
from app.persistence import UnitOfWork
from app.reconciler import Reconciler
from app.store.memory import InMemoryDatabase
from app.store.records import ResourceQuotaRecord


class Ci:
    mode = "recording"

    def __init__(self, status):
        self.status = status
        self.aborted = []

    def launch(self, request):
        return None

    def get_status(self, external_id):
        return self.status

    def abort(self, external_id):
        self.aborted.append(external_id)


def _platform():
    platform = DeliveryPlatform(database=InMemoryDatabase())
    application = platform.create_application(
        name=f"co-{uuid.uuid4().hex[:6]}", repository_url="https://git.example/co",
        pipeline_template="container-ci-cd-v1", runtime=Runtime.DOCKER, default_environment=Environment.DEV,
        stages=[], idempotency_key=uuid.uuid4().hex)
    return platform, application


def _start(platform, application):
    return platform.start_pipeline(application.id, commit_sha="a" * 40, branch="main", environment=Environment.DEV,
                                   parameters={}, correlation_id="c", idempotency_key=uuid.uuid4().hex)


def _age(platform, run, seconds):
    """Make an admitted run look `seconds` old since it reached CI."""

    current = platform.get_pipeline(run.id)
    past = utc_now() - timedelta(seconds=seconds)
    aged = replace(current, admitted_at=past, created_at=past, version=current.version + 1)
    with platform.transaction() as tx:
        tx.apply(UnitOfWork(runs=[(aged, current.version)]))


# ------------------------------------------------------------------ the reconciler


def test_a_run_waiting_for_admission_is_never_timed_out():
    platform, application = _platform()
    with platform.transaction() as tx:
        tx.set_resource_quota(ResourceQuotaRecord(id=uuid.uuid4(), scope="application", scope_id=str(application.id),
                                                  max_concurrent_pipelines=1))
    _start(platform, application)
    waiting = _start(platform, application)
    old = replace(platform.get_pipeline(waiting.id), created_at=utc_now() - timedelta(hours=5))
    with platform.transaction() as tx:
        tx.apply(UnitOfWork(runs=[(replace(old, version=old.version + 1), old.version)]))

    Reconciler(platform, Ci(None), None).reconcile_runs(timeout_seconds=3600)
    assert platform.get_pipeline(waiting.id).status == PipelineStatus.QUEUED


def test_a_build_jenkins_still_runs_is_not_failed_for_being_long():
    platform, application = _platform()
    run = _start(platform, application)
    platform.record_ci_result(run.id, "running", None, [])
    _age(platform, run, 3 * 3600)
    ci = Ci("running")
    Reconciler(platform, ci, None).reconcile_runs(timeout_seconds=3600)
    assert platform.get_pipeline(run.id).status == PipelineStatus.RUNNING and ci.aborted == []


def test_a_build_past_its_callback_token_is_failed_and_stopped():
    platform, application = _platform()
    run = _start(platform, application)
    platform.record_ci_result(run.id, "running", None, [])
    _age(platform, run, workload_identity.MAX_TTL_SECONDS + 60)
    ci = Ci("running")
    Reconciler(platform, ci, None).reconcile_runs(timeout_seconds=3600)
    assert platform.get_pipeline(run.id).status == PipelineStatus.FAILED
    assert len(ci.aborted) == 1


def test_a_build_jenkins_does_not_know_is_failed_after_the_timeout_and_stopped():
    platform, application = _platform()
    run = _start(platform, application)
    platform.record_ci_result(run.id, "running", None, [])
    _age(platform, run, 3700)
    ci = Ci(None)
    Reconciler(platform, ci, None).reconcile_runs(timeout_seconds=3600)
    assert platform.get_pipeline(run.id).status == PipelineStatus.FAILED and len(ci.aborted) == 1


# ------------------------------------------------------------------ agentLabel


def test_choosing_a_pod_template_is_refused_unless_the_operator_allows_it(monkeypatch):
    monkeypatch.delenv("NETCI_ALLOWED_AGENT_LABELS", raising=False)
    with pytest.raises(BuildInputError) as refused:
        validate_build_inputs({"agentLabel": "netci-shared"})
    assert refused.value.code == "AGENT_LABEL_NOT_ALLOWED"

    monkeypatch.setenv("NETCI_ALLOWED_AGENT_LABELS", "netci-ephemeral, netci-gpu")
    assert validate_build_inputs({"agentLabel": "netci-gpu"})["agentLabel"] == "netci-gpu"
    with pytest.raises(BuildInputError):
        validate_build_inputs({"agentLabel": "netci-shared"})
    with pytest.raises(BuildInputError) as bad:
        validate_build_inputs({"agentLabel": "x' ; evil"})
    assert bad.value.code == "BUILD_INPUT_INVALID"


# ------------------------------------------------------------------ the ci-report callback


def test_a_ci_report_needs_a_token_for_that_modules_application(monkeypatch):
    from test_ci_cd_separation import _module

    monkeypatch.setenv("NETCI_WORKLOAD_TOKEN_KEYS", "k1:" + "q" * 48)
    main_mod.platform.reset()
    client = TestClient(main_mod.app)
    module, _ = _module("report-api")
    other, _ = _module("other-api")

    def token(application_id, scopes):
        return workload_identity.mint(workload=workload_identity.Workload.JENKINS,
                                      application_id=uuid.UUID(application_id), pipeline_run_id=uuid.uuid4(),
                                      scopes=scopes, ttl_seconds=600)

    body = {"coverage": 81.5, "autoTest": "passed", "sast": "passed", "sastIssues": 0,
            "vulnerabilities": {"critical": 0, "high": 0, "medium": 1}, "commit": "a" * 40}
    path = f"/modules/{module['id']}/versions/v1.0.0/ci-report"
    foreign = client.post(path, json=body, headers={"Authorization": "Bearer " + token(
        other["applicationId"], {workload_identity.Scope.CI_REPORT})})
    assert foreign.status_code == 403, foreign.text
    wrong_scope = client.post(path, json=body, headers={"Authorization": "Bearer " + token(
        module["applicationId"], {workload_identity.Scope.CI_STAGE})})
    assert wrong_scope.status_code == 403, wrong_scope.text
    # The right token passes authorization; what the portal then does with a version
    # that does not exist is its own answer, not a 403.
    own = client.post(path, json=body, headers={"Authorization": "Bearer " + token(
        module["applicationId"], {workload_identity.Scope.CI_REPORT})})
    assert own.status_code != 403, own.text


# ------------------------------------------------------------------ fail closed


def test_outside_local_mode_the_controllers_and_callback_url_must_be_set(monkeypatch):
    from app.adapters.ci_launcher import build_ci_launcher

    monkeypatch.setenv("NETCI_ENVIRONMENT", "production")
    monkeypatch.setenv("NETCI_CI_MODE", "jenkins")
    monkeypatch.delenv("NETCI_JENKINS_CONTROLLERS", raising=False)
    monkeypatch.delenv("NETCI_CALLBACK_URL", raising=False)
    with pytest.raises(ValueError, match="NETCI_JENKINS_CONTROLLERS, NETCI_CALLBACK_URL"):
        build_ci_launcher()


def test_the_admin_level_drift_probe_can_be_turned_off(monkeypatch):
    class Launcher:
        def controller_drift(self):
            raise AssertionError("must not be called")

    monkeypatch.setattr(main_mod.platform, "ci_launcher", Launcher())
    monkeypatch.setenv("NETCI_CONTROLLER_DRIFT_PROBE", "false")
    main_mod._drift_observation["value"] = None
    assert main_mod._controller_drift_observation() is None
