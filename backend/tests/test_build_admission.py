"""Builds are admitted and superseded, not refused (ADR-050).

An agent pushing twenty commits to one pull request must cost about two builds, not
twenty -- without ever stopping a run that has begun to sign, publish or deploy, and
without ever skipping a release.
"""

from __future__ import annotations

import os
import threading
import uuid
from datetime import datetime, timezone

import pytest

from app.adapters.ci_launcher import LaunchedCi
from app.delivery import DeliveryError, DeliveryPlatform
from app.domain.delivery_rules import DeliveryRuleError, ScmEvent, decide, parse_rules
from app.domain.models import Environment, PipelineStatus, Runtime
from app.store.memory import InMemoryDatabase
from app.store.records import ResourceQuotaRecord


class Launcher:
    mode = "recording"

    def __init__(self):
        self.launched: list[uuid.UUID] = []
        self.aborted: list[str] = []
        self._lock = threading.Lock()

    def launch(self, request):
        with self._lock:
            self.launched.append(request.pipeline_run_id)
            number = len(self.launched)
        return LaunchedCi(controller_id="jenkins-a", external_run_id=f"job#{number}")

    def abort(self, jenkins_run_id):
        self.aborted.append(jenkins_run_id)

    def get_status(self, jenkins_run_id):
        return None


def _platform(launcher=None, database=None):
    return DeliveryPlatform(database=database or InMemoryDatabase(), ci_launcher=launcher or Launcher())


def _app(platform, name="agent-app"):
    return platform.create_application(
        name=f"{name}-{uuid.uuid4().hex[:6]}", repository_url="https://git.example/app",
        pipeline_template="container-ci-cd-v1", runtime=Runtime.DOCKER, default_environment=Environment.DEV,
        stages=[], idempotency_key=uuid.uuid4().hex,
    )


def _quota(platform, application, *, pipelines, queued=50):
    with platform.transaction() as tx:
        tx.set_resource_quota(ResourceQuotaRecord(
            id=uuid.uuid4(), scope="application", scope_id=str(application.id),
            max_concurrent_pipelines=pipelines, max_queued_pipelines=queued,
        ))


def _pr_push(platform, application, sha, *, number=12, cancel_in_progress=True):
    return platform.start_pipeline(
        application.id, commit_sha=sha, branch="agent/fix", environment=Environment.DEV, parameters={},
        correlation_id="c", idempotency_key=uuid.uuid4().hex, deploy_after_build=False,
        trigger={"event": "pull_request", "pullRequest": number, "branch": "agent/fix"},
        cancel_in_progress=cancel_in_progress,
    )


def _branch_push(platform, application, sha, *, cancel_in_progress=False):
    return platform.start_pipeline(
        application.id, commit_sha=sha, branch="main", environment=Environment.DEV, parameters={},
        correlation_id="c", idempotency_key=uuid.uuid4().hex,
        trigger={"event": "push", "branch": "main"}, cancel_in_progress=cancel_in_progress,
    )


def _manual(platform, application, sha="m" * 40):
    return platform.start_pipeline(
        application.id, commit_sha=sha, branch="main", environment=Environment.DEV, parameters={},
        correlation_id="c", idempotency_key=uuid.uuid4().hex, trigger={"event": "manual"},
    )


def _audits(platform, run_id, action):
    with platform.transaction() as tx:
        return [a for a in tx.audit_records() if a.event_type == action and a.pipeline_run_id == run_id]


def test_a_waiting_pull_request_run_is_superseded_by_the_next_push():
    platform = _platform()
    application = _app(platform)
    _quota(platform, application, pipelines=1)
    _manual(platform, application)  # holds the only slot
    first = _pr_push(platform, application, "a" * 40)
    assert first.admitted_at is None

    second = _pr_push(platform, application, "b" * 40)

    first_now = platform.get_pipeline(first.id)
    assert first_now.status == PipelineStatus.CANCELLED and first_now.superseded_by == second.id
    assert platform.get_pipeline(second.id).status == PipelineStatus.QUEUED
    assert _audits(platform, first.id, "pipeline.superseded")[0].payload["wasAdmitted"] is False


def test_twenty_pushes_to_one_pull_request_leave_one_run_building_and_one_waiting():
    launcher = Launcher()
    platform = _platform(launcher)
    application = _app(platform)
    _quota(platform, application, pipelines=1)
    runs = [_pr_push(platform, application, f"{i:040x}", cancel_in_progress=False) for i in range(20)]

    statuses = [platform.get_pipeline(run.id) for run in runs]
    alive = [run for run in statuses if run.status != PipelineStatus.CANCELLED]
    assert [run.id for run in alive] == [runs[0].id, runs[-1].id]
    assert alive[0].admitted_at is not None and alive[1].admitted_at is None
    assert launcher.launched == [runs[0].id]


def test_a_building_pull_request_run_is_cancelled_and_its_ci_build_stopped():
    launcher = Launcher()
    platform = _platform(launcher)
    application = _app(platform)
    first = _pr_push(platform, application, "a" * 40)
    platform.record_ci_result(first.id, PipelineStatus.RUNNING.value, None, [])

    second = _pr_push(platform, application, "b" * 40)

    assert platform.get_pipeline(first.id).status == PipelineStatus.CANCELLED
    assert launcher.aborted == [platform.get_pipeline(first.id).jenkins_run_id]
    assert platform.get_pipeline(second.id).admitted_at is not None


def test_a_building_branch_run_is_left_to_finish_by_default():
    platform = _platform()
    application = _app(platform)
    first = _branch_push(platform, application, "a" * 40)
    platform.record_ci_result(first.id, PipelineStatus.RUNNING.value, None, [])

    _branch_push(platform, application, "b" * 40)

    assert platform.get_pipeline(first.id).status == PipelineStatus.RUNNING


@pytest.mark.parametrize("stage", ["sign", "publish", "deploy", "health-check"])
def test_a_run_that_began_signing_publishing_or_deploying_is_never_superseded(stage):
    launcher = Launcher()
    platform = _platform(launcher)
    application = _app(platform)
    first = _pr_push(platform, application, "a" * 40)
    platform.record_ci_result(first.id, PipelineStatus.RUNNING.value, None, [])
    platform.record_stage_event(first.id, stage_id=stage, stage_name=stage, status="running",
                                started_at=datetime.now(timezone.utc))

    _pr_push(platform, application, "b" * 40)

    assert platform.get_pipeline(first.id).status == PipelineStatus.RUNNING
    assert launcher.aborted == []


def test_a_run_before_its_point_of_no_return_is_superseded():
    platform = _platform()
    application = _app(platform)
    first = _pr_push(platform, application, "a" * 40)
    platform.record_ci_result(first.id, PipelineStatus.RUNNING.value, None, [])
    platform.record_stage_event(first.id, stage_id="unit-test", stage_name="Unit tests", status="running",
                                started_at=datetime.now(timezone.utc))

    _pr_push(platform, application, "b" * 40)

    assert platform.get_pipeline(first.id).status == PipelineStatus.CANCELLED


def test_manual_and_tag_runs_belong_to_no_group_and_are_never_superseded():
    platform = _platform()
    application = _app(platform)
    manual = _manual(platform, application, "a" * 40)
    tag = platform.start_pipeline(
        application.id, commit_sha="b" * 40, branch="main", environment=Environment.DEV, parameters={},
        correlation_id="c", idempotency_key=uuid.uuid4().hex, release_tag="v1.2.0",
        trigger={"event": "tag", "tag": "v1.2.0"},
    )
    assert manual.concurrency_group is None and tag.concurrency_group is None

    _branch_push(platform, application, "c" * 40, cancel_in_progress=True)
    _manual(platform, application, "d" * 40)

    assert platform.get_pipeline(manual.id).status != PipelineStatus.CANCELLED
    assert platform.get_pipeline(tag.id).status != PipelineStatus.CANCELLED


def test_pull_requests_are_separate_groups():
    platform = _platform()
    application = _app(platform)
    _quota(platform, application, pipelines=1)
    _manual(platform, application)
    twelve = _pr_push(platform, application, "a" * 40, number=12)
    _pr_push(platform, application, "b" * 40, number=13)
    assert platform.get_pipeline(twelve.id).status == PipelineStatus.QUEUED


def test_a_late_callback_from_a_superseded_run_is_refused():
    platform = _platform()
    application = _app(platform)
    first = _pr_push(platform, application, "a" * 40)
    platform.record_ci_result(first.id, PipelineStatus.RUNNING.value, None, [])
    _pr_push(platform, application, "b" * 40)

    with pytest.raises(DeliveryError) as refused:
        platform.record_ci_result(first.id, PipelineStatus.SUCCEEDED.value, "sha256:" + "e" * 64, [])
    assert refused.value.status_code == 409
    assert platform.get_pipeline(first.id).status == PipelineStatus.CANCELLED


def test_a_push_that_supersedes_the_waiting_run_is_accepted_even_when_the_queue_is_full():
    platform = _platform()
    application = _app(platform)
    _quota(platform, application, pipelines=1, queued=1)
    _manual(platform, application)
    _pr_push(platform, application, "a" * 40)
    # The queue is full, but this push replaces the one waiting in it.
    replacement = _pr_push(platform, application, "b" * 40)
    assert replacement.status == PipelineStatus.QUEUED
    with pytest.raises(DeliveryError) as refused:
        _pr_push(platform, application, "c" * 40, number=99)
    assert refused.value.code == "PIPELINE_QUEUE_FULL"


def test_the_superseded_commit_is_told_so():
    from app.domain.models import ScmIntegration, ScmProviderType

    platform = _platform()
    application = _app(platform)
    with platform.transaction() as tx:
        tx.upsert_scm_integration(ScmIntegration(application_id=application.id, provider=ScmProviderType.GITHUB,
                                                 repository_identity="acme/app"))
    first = _pr_push(platform, application, "a" * 40)
    platform.record_ci_result(first.id, PipelineStatus.RUNNING.value, None, [])
    _pr_push(platform, application, "b" * 40)
    with platform.transaction() as tx:
        statuses = [n for n in tx.pending_notifications(limit=100)
                    if n.event_type == "scm.commit_status" and n.payload.get("commitSha") == "a" * 40]
    assert statuses and statuses[-1].payload["state"] == "cancelled"


# ------------------------------------------------------------------ delivery rules


def test_cancel_in_progress_defaults_to_pull_requests_and_can_be_set_per_trigger():
    rules = parse_rules({"triggers": [
        {"on": "pull_request", "branches": ["**"]},
        {"on": "push", "branches": ["main"]},
        {"on": "push", "branches": ["agent/**"], "cancelInProgress": True},
    ]}, default_environment=Environment.DEV, configured=[Environment.DEV])
    pr = decide(rules, ScmEvent(kind="pull_request", branch="main"))
    main = decide(rules, ScmEvent(kind="push", branch="main"))
    agent = decide(rules, ScmEvent(kind="push", branch="agent/refactor"))
    assert (pr.cancel_in_progress, main.cancel_in_progress, agent.cancel_in_progress) == (True, False, True)


@pytest.mark.parametrize("trigger, message", [
    ({"on": "push", "branches": ["main"], "cancelInProgress": "yes"}, "must be true or false"),
    ({"on": "tag", "tags": ["v*"], "cancelInProgress": True}, "never superseded"),
])
def test_cancel_in_progress_is_validated(trigger, message):
    with pytest.raises(DeliveryRuleError, match=message):
        parse_rules({"triggers": [trigger]}, default_environment=Environment.DEV, configured=[Environment.DEV])


# ------------------------------------------------------------------ PostgreSQL

DATABASE_URL = os.getenv("NETCI_TEST_DATABASE_URL", "").strip()


@pytest.mark.skipif(not DATABASE_URL, reason="set NETCI_TEST_DATABASE_URL to race against PostgreSQL")
def test_concurrent_pushes_to_one_pull_request_leave_exactly_one_waiting(monkeypatch):
    from test_persistence_postgres import truncate

    monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
    truncate()
    try:
        platform = DeliveryPlatform()
        application = _app(platform, "pg-race")
        _quota(platform, application, pipelines=1)
        _manual(platform, application)
        barrier = threading.Barrier(6)
        errors: list[BaseException] = []

        def push(index):
            try:
                replica = DeliveryPlatform()
                barrier.wait()
                _pr_push(replica, application, f"{index:040x}")
            except BaseException as exc:  # noqa: BLE001 - asserted below
                errors.append(exc)

        threads = [threading.Thread(target=push, args=(i,)) for i in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        assert errors == []
        runs = [r for r in platform.list_pipeline_runs(application.id) if r.concurrency_group]
        waiting = [r for r in runs if r.status == PipelineStatus.QUEUED]
        assert len(runs) == 6 and len(waiting) == 1
        assert all(r.superseded_by is not None for r in runs if r.status == PipelineStatus.CANCELLED)
    finally:
        truncate()
