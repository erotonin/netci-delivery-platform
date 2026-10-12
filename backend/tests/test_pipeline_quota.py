"""Concurrent pipeline quotas, counted over the quota's own scope and safe under a race.

`check_pipeline_quota` existed and nothing called it, so the limit was documentation. When
it is enforced it must count the right runs -- a team quota covers every application of
the team -- and two starts at once must not both see room for one.

Since ADR-050 the limit admits instead of refusing: a run over it waits, queued and not yet
admitted, and is dispatched when a slot frees. Only a full queue is refused.
"""

from __future__ import annotations

import os
import threading
import uuid

import pytest

from app.delivery import DeliveryError, DeliveryPlatform
from app.domain.models import Environment, PipelineStatus, Runtime
from app.store.memory import InMemoryDatabase
from app.store.records import ResourceQuotaRecord


def _platform(database=None) -> DeliveryPlatform:
    return DeliveryPlatform(database=database or InMemoryDatabase())


def _app(platform, name, team=None):
    return platform.create_application(
        name=name, repository_url=f"https://git.example/{name}", pipeline_template="container-ci-cd-v1",
        runtime=Runtime.DOCKER, default_environment=Environment.DEV, stages=[], owner_team=team,
        idempotency_key=f"create-{name}-{uuid.uuid4().hex[:6]}",
    )


def _start(platform, application):
    return platform.start_pipeline(application.id, commit_sha="abc1234", branch="main", environment=Environment.DEV,
                                   parameters={}, correlation_id="q", idempotency_key=uuid.uuid4().hex)


def _quota(platform, scope, scope_id, pipelines, queued=50):
    with platform.transaction() as tx:
        tx.set_resource_quota(ResourceQuotaRecord(id=uuid.uuid4(), scope=scope, scope_id=scope_id,
                                                  max_concurrent_pipelines=pipelines, max_queued_pipelines=queued))


def _admitted(run) -> bool:
    return run.admitted_at is not None


def _waits(platform, application):
    run = _start(platform, application)
    assert run.status == PipelineStatus.QUEUED and not _admitted(run), "over the limit, a run waits"
    return run


def test_the_built_in_default_applies_per_application_not_platform_wide():
    platform = _platform()
    a, b = _app(platform, "quota-a"), _app(platform, "quota-b")
    for _ in range(5):
        assert _admitted(_start(platform, a))
    _waits(platform, a)
    # Counted platform-wide, the default would have held b back too.
    assert _admitted(_start(platform, b))


def test_a_team_quota_counts_every_application_of_the_team():
    platform = _platform()
    a, b = _app(platform, "team-a", team="payments"), _app(platform, "team-b", team="payments")
    other = _app(platform, "other-c", team="search")
    _quota(platform, "team", "payments", 2)
    assert _admitted(_start(platform, a)) and _admitted(_start(platform, b))
    _waits(platform, a)
    _waits(platform, b)
    assert _admitted(_start(platform, other))


def test_a_configured_global_quota_counts_the_whole_platform():
    platform = _platform()
    a, b = _app(platform, "global-a"), _app(platform, "global-b")
    _quota(platform, "global", "global", 2)
    _start(platform, a)
    _start(platform, b)
    _waits(platform, b)


def test_a_finished_run_admits_the_oldest_waiting_one_and_a_retry_waits_its_turn():
    platform = _platform()
    a = _app(platform, "slot-a")
    _quota(platform, "application", str(a.id), 1)
    run = _start(platform, a)
    first_waiting = _waits(platform, a)
    second_waiting = _waits(platform, a)

    platform.record_ci_result(run.id, PipelineStatus.RUNNING.value, None, [])
    platform.record_ci_result(run.id, PipelineStatus.FAILED.value, None, [])

    assert _admitted(platform.get_pipeline(first_waiting.id)), "oldest first"
    assert not _admitted(platform.get_pipeline(second_waiting.id))
    retry = platform.retry_pipeline(run.id, actor="dev1")
    assert retry.retry_of == run.id and not _admitted(retry)


def test_a_run_waiting_for_approval_no_longer_holds_a_ci_slot():
    # It has left Jenkins. Counting it would let one unanswered approval stop every build.
    platform = _platform()
    a = _app(platform, "approval-a")
    _quota(platform, "application", str(a.id), 1)
    run = platform.start_pipeline(a.id, commit_sha="abc1234", branch="main", environment=Environment.PROD,
                                  parameters={}, correlation_id="q", idempotency_key=uuid.uuid4().hex)
    waiting = _waits(platform, a)
    platform.record_ci_result(run.id, PipelineStatus.RUNNING.value, None, [])
    platform.record_ci_result(run.id, PipelineStatus.SUCCEEDED.value, "sha256:" + "d" * 64, [])
    assert platform.get_pipeline(run.id).status == PipelineStatus.WAITING_APPROVAL
    assert _admitted(platform.get_pipeline(waiting.id))


def test_only_a_full_queue_is_refused():
    platform = _platform()
    a = _app(platform, "full-a")
    _quota(platform, "application", str(a.id), 1, queued=2)
    _start(platform, a)
    _waits(platform, a)
    _waits(platform, a)
    with pytest.raises(DeliveryError) as refused:
        _start(platform, a)
    assert refused.value.code == "PIPELINE_QUEUE_FULL" and refused.value.status_code == 429


DATABASE_URL = os.getenv("NETCI_TEST_DATABASE_URL", "").strip()


@pytest.mark.skipif(not DATABASE_URL, reason="set NETCI_TEST_DATABASE_URL to race against PostgreSQL")
def test_two_starts_racing_for_the_last_slot_admit_exactly_one(monkeypatch):
    from test_persistence_postgres import truncate

    monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
    truncate()
    try:
        platform = DeliveryPlatform()
        application = _app(platform, "race-a")
        _quota(platform, "application", str(application.id), 1)
        barrier = threading.Barrier(4)
        outcomes: list[str] = []

        def start():
            barrier.wait()
            try:
                run = DeliveryPlatform().start_pipeline(
                    application.id, commit_sha="abc1234", branch="main", environment=Environment.DEV,
                    parameters={}, correlation_id="race", idempotency_key=uuid.uuid4().hex)
                outcomes.append("admitted" if run.admitted_at is not None else "waiting")
            except DeliveryError as exc:
                outcomes.append(exc.code)

        threads = [threading.Thread(target=start) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        assert sorted(outcomes) == ["admitted"] + ["waiting"] * 3
        with platform.transaction() as tx:
            assert tx.count_admitted_pipeline_runs([application.id]) == 1
    finally:
        truncate()


@pytest.mark.skipif(not DATABASE_URL, reason="set NETCI_TEST_DATABASE_URL to race against PostgreSQL")
def test_replicas_admitting_at_once_never_dispatch_one_run_twice(monkeypatch):
    from test_persistence_postgres import truncate
    from app.adapters.ci_launcher import LaunchedCi

    class CountingLauncher:
        mode = "counting"

        def __init__(self):
            self.launched: list[uuid.UUID] = []
            self._lock = threading.Lock()

        def launch(self, request):
            with self._lock:
                self.launched.append(request.pipeline_run_id)
            return LaunchedCi(controller_id="jenkins-a", external_run_id=f"job#{len(self.launched)}")

        def abort(self, jenkins_run_id):
            return None

        def get_status(self, jenkins_run_id):
            return None

    monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
    truncate()
    try:
        writer = DeliveryPlatform()
        application = _app(writer, "admit-race")
        _quota(writer, "application", str(application.id), 1)
        blocker = _start(writer, application)
        waiting = [_start(writer, application) for _ in range(6)]
        assert not any(run.admitted_at for run in waiting)
        _quota(writer, "application", str(application.id), 4)

        launcher = CountingLauncher()
        barrier = threading.Barrier(5)

        def admit():
            replica = DeliveryPlatform(ci_launcher=launcher)
            barrier.wait()
            replica.admit_waiting_runs()

        threads = [threading.Thread(target=admit) for _ in range(5)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        assert len(launcher.launched) == len(set(launcher.launched)) == 3, launcher.launched
        assert blocker.id not in launcher.launched
        with writer.transaction() as tx:
            assert tx.count_admitted_pipeline_runs([application.id]) == 4
    finally:
        truncate()
