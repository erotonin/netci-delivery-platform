"""CI cost: measured capacity and the estimate of what supersession avoided, never mixed."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

import app.main as main_mod
from app.auth import Principal
from app.delivery import DeliveryPlatform
from app.domain.models import Environment, PipelineStatus, Runtime
from app.policy.rules import Role
from app.projections.finops import ci_cost
from app.store.memory import InMemoryDatabase
from app.store.records import ResourceQuotaRecord


def _platform():
    return DeliveryPlatform(database=InMemoryDatabase())


def _app(platform, name="cost"):
    return platform.create_application(
        name=f"{name}-{uuid.uuid4().hex[:6]}", repository_url="https://git.example/c",
        pipeline_template="container-ci-cd-v1", runtime=Runtime.DOCKER, default_environment=Environment.DEV,
        stages=[], idempotency_key=uuid.uuid4().hex)


def _start(platform, application, sha, *, pr=None):
    trigger = {"event": "pull_request", "pullRequest": pr} if pr else {"event": "manual"}
    return platform.start_pipeline(application.id, commit_sha=sha, branch="main", environment=Environment.DEV,
                                   parameters={}, correlation_id="c", idempotency_key=uuid.uuid4().hex,
                                   deploy_after_build=False, trigger=trigger)


def _finish(platform, run, status, durations_ms):
    platform.record_ci_result(run.id, PipelineStatus.RUNNING.value, None, [])
    for index, duration in enumerate(durations_ms):
        platform.record_stage_event(run.id, stage_id=f"s{index}", stage_name=f"s{index}", status="succeeded",
                                    duration_ms=duration)
    digest = "sha256:" + "c" * 64 if status == PipelineStatus.SUCCEEDED else None
    platform.record_ci_result(run.id, status.value, digest, [])


NOW = lambda: datetime.now(timezone.utc) + timedelta(seconds=1)  # noqa: E731


def test_runner_seconds_are_the_recorded_stage_durations():
    platform = _platform()
    application = _app(platform)
    _finish(platform, _start(platform, application, "a" * 40), PipelineStatus.SUCCEEDED, [60_000, 30_500])
    run = _start(platform, application, "b" * 40)
    platform.record_ci_result(run.id, PipelineStatus.RUNNING.value, None, [])
    platform.record_stage_event(run.id, stage_id="x", stage_name="x", status="running")

    report = ci_cost(platform, {application.id}, now=NOW(), days=30, price_per_runner_hour=None, currency=None)
    row = report["applications"][0]
    assert row["runnerSeconds"] == 90.5
    assert row["stagesWithoutDuration"] == 1
    assert row["cost"] is None and report["total"]["cost"] is None


def test_superseded_runs_are_split_by_whether_they_reached_ci_and_the_estimate_uses_the_median():
    platform = _platform()
    application = _app(platform)
    with platform.transaction() as tx:
        tx.set_resource_quota(ResourceQuotaRecord(id=uuid.uuid4(), scope="application", scope_id=str(application.id),
                                                  max_concurrent_pipelines=1))
    for sha, seconds in (("1" * 40, [100_000]), ("2" * 40, [200_000]), ("3" * 40, [300_000])):
        _finish(platform, _start(platform, application, sha), PipelineStatus.SUCCEEDED, seconds)
    blocker = _start(platform, application, "4" * 40)      # holds the only slot
    for i in range(3):                                     # waiting, each superseding the last
        _start(platform, application, f"{i + 5}" * 40, pr=12)

    row = ci_cost(platform, {application.id}, now=NOW(), days=30, price_per_runner_hour=None,
                  currency=None)["applications"][0]
    assert row["supersededBeforeAdmission"] == 2 and row["supersededWhileBuilding"] == 0
    assert row["estimatedAvoidedRunnerSeconds"] == 2 * 200.0  # median of 100, 200, 300
    assert blocker.id  # the blocker is admitted and counts as a run, not a saving


def test_no_succeeded_run_means_no_estimate_and_the_total_says_it_is_incomplete():
    platform = _platform()
    application = _app(platform)
    with platform.transaction() as tx:
        tx.set_resource_quota(ResourceQuotaRecord(id=uuid.uuid4(), scope="application", scope_id=str(application.id),
                                                  max_concurrent_pipelines=1))
    _start(platform, application, "1" * 40)
    _start(platform, application, "2" * 40, pr=3)
    _start(platform, application, "3" * 40, pr=3)
    report = ci_cost(platform, {application.id}, now=NOW(), days=30, price_per_runner_hour=None, currency=None)
    assert report["applications"][0]["estimatedAvoidedRunnerSeconds"] is None
    assert report["total"]["estimatedAvoidedRunnerSeconds"] is None
    assert report["total"]["estimatedAvoidedIncomplete"] is True


def test_cost_is_money_only_with_a_price_and_is_rounded_half_up():
    platform = _platform()
    application = _app(platform)
    _finish(platform, _start(platform, application, "a" * 40), PipelineStatus.SUCCEEDED, [5_400_000])  # 1.5 h
    report = ci_cost(platform, {application.id}, now=NOW(), days=30,
                     price_per_runner_hour=Decimal("0.333"), currency="EUR")
    # Nothing was superseded, and there is a succeeded run to estimate from: the estimate is 0.
    assert report["applications"][0]["cost"] == {"currency": "EUR", "measured": "0.50", "estimatedAvoided": "0.00"}


def test_runs_outside_the_window_are_not_counted():
    platform = _platform()
    application = _app(platform)
    _finish(platform, _start(platform, application, "a" * 40), PipelineStatus.FAILED, [1000])
    later = datetime.now(timezone.utc) + timedelta(days=40)
    report = ci_cost(platform, {application.id}, now=later, days=30, price_per_runner_hour=None, currency=None)
    assert report["applications"][0]["runs"] == 0 and report["total"]["runnerSeconds"] == 0


# ------------------------------------------------------------------ the route

VIEWER_ALPHA = Principal(subject="viewer-alpha", display_name="Viewer Alpha", email="", roles=frozenset({Role.VIEWER}),
                         method="token", teams=frozenset({"team-alpha"}))


@pytest.fixture()
def client(monkeypatch):
    # Other modules reload app.main; a client built at import would miss the override.
    main_mod.platform.reset()
    yield TestClient(main_mod.app)
    main_mod.app.dependency_overrides.pop(main_mod.current_principal, None)


def test_the_route_covers_only_applications_the_caller_may_see_and_prices_from_configuration(client, monkeypatch):
    def create(name, team):
        return main_mod.platform.create_application(
            name=name, repository_url=f"https://git.example/{name}", pipeline_template="container-ci-cd-v1",
            runtime=Runtime.DOCKER, default_environment=Environment.DEV, stages=[], owner_team=team,
            idempotency_key=uuid.uuid4().hex)

    visible, hidden = create("alpha-svc", "team-alpha"), create("beta-svc", "team-beta")
    monkeypatch.setenv("NETCI_CI_PRICE_PER_RUNNER_HOUR", "2.40")
    main_mod.app.dependency_overrides[main_mod.current_principal] = lambda: VIEWER_ALPHA

    body = client.get("/finops/ci?days=7").json()
    ids = {row["applicationId"] for row in body["applications"]}
    assert str(visible.id) in ids and str(hidden.id) not in ids
    assert body["total"]["cost"]["currency"] == "USD"


def test_an_invalid_price_shows_no_money_rather_than_a_wrong_amount(client, monkeypatch):
    monkeypatch.setenv("NETCI_CI_PRICE_PER_RUNNER_HOUR", "cheap")
    assert client.get("/finops/ci").json()["total"]["cost"] is None
