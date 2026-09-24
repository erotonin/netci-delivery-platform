"""Concurrent pipeline quotas, counted over the quota's own scope and safe under a race.

`check_pipeline_quota` existed and nothing called it, so the limit was documentation. When
it is enforced it must count the right runs -- a team quota covers every application of
the team -- and two starts at once must not both see room for one.
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


def _quota(platform, scope, scope_id, pipelines):
    with platform.transaction() as tx:
        tx.set_resource_quota(ResourceQuotaRecord(id=uuid.uuid4(), scope=scope, scope_id=scope_id,
                                                  max_concurrent_pipelines=pipelines))


def _refused(platform, application):
    with pytest.raises(DeliveryError) as refused:
        _start(platform, application)
    assert refused.value.code == "PIPELINE_QUOTA_EXCEEDED" and refused.value.status_code == 429
    return refused.value


def test_the_built_in_default_applies_per_application_not_platform_wide():
    platform = _platform()
    a, b = _app(platform, "quota-a"), _app(platform, "quota-b")
    for _ in range(5):
        _start(platform, a)
    _refused(platform, a)
    # Counted platform-wide, the default would have stopped b too.
    _start(platform, b)


def test_a_team_quota_counts_every_application_of_the_team():
    platform = _platform()
    a, b = _app(platform, "team-a", team="payments"), _app(platform, "team-b", team="payments")
    other = _app(platform, "other-c", team="search")
    _quota(platform, "team", "payments", 2)
    _start(platform, a)
    _start(platform, b)
    _refused(platform, a)
    _refused(platform, b)
    _start(platform, other)


def test_a_configured_global_quota_counts_the_whole_platform():
    platform = _platform()
    a, b = _app(platform, "global-a"), _app(platform, "global-b")
    _quota(platform, "global", "global", 2)
    _start(platform, a)
    _start(platform, b)
    _refused(platform, b)


def test_a_finished_run_frees_its_slot_and_a_retry_takes_one():
    platform = _platform()
    a = _app(platform, "slot-a")
    _quota(platform, "application", str(a.id), 1)
    run = _start(platform, a)
    _refused(platform, a)
    platform.record_ci_result(run.id, PipelineStatus.RUNNING.value, None, [])
    platform.record_ci_result(run.id, PipelineStatus.FAILED.value, None, [])
    retry = platform.retry_pipeline(run.id, actor="dev1")
    assert retry.retry_of == run.id
    _refused(platform, a)


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
                DeliveryPlatform().start_pipeline(
                    application.id, commit_sha="abc1234", branch="main", environment=Environment.DEV,
                    parameters={}, correlation_id="race", idempotency_key=uuid.uuid4().hex)
                outcomes.append("started")
            except DeliveryError as exc:
                outcomes.append(exc.code)

        threads = [threading.Thread(target=start) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        assert sorted(outcomes) == ["PIPELINE_QUOTA_EXCEEDED"] * 3 + ["started"]
    finally:
        truncate()
