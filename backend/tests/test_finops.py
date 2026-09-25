"""CI cost: measured capacity and the estimate of what supersession avoided, never mixed."""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

import app.main as main_mod
from app.auth import Principal
from app.delivery import DeliveryPlatform
from app.domain.models import Environment, PipelineRun, PipelineStatus, Runtime
from app.policy.rules import Role
from app.projections.finops import ci_cost
from app.store.memory import InMemoryDatabase
from app.persistence import UnitOfWork


T0 = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)
NOW = T0 + timedelta(days=1)
DIGEST = "sha256:" + "c" * 64


def _platform():
    return DeliveryPlatform(database=InMemoryDatabase())


def _app(platform, name="cost"):
    return platform.create_application(
        name=f"{name}-{uuid.uuid4().hex[:6]}", repository_url="https://git.example/c",
        pipeline_template="container-ci-cd-v1", runtime=Runtime.DOCKER, default_environment=Environment.DEV,
        stages=[], idempotency_key=uuid.uuid4().hex)


def _run(platform, application, *, status, held=None, admitted=True, digest=None, superseded_by=None,
         finished=True, created=T0):
    """A run as the store would hold it, with its CI timestamps fixed."""

    admitted_at = created + timedelta(seconds=5) if admitted else None
    ci_finished_at = admitted_at + timedelta(seconds=held) if (admitted and finished and held is not None) else None
    run = PipelineRun(application_id=application.id, commit_sha=uuid.uuid4().hex + "00000000", branch="main",
                      environment=Environment.DEV, status=status, artifact_digest=digest,
                      admitted_at=admitted_at, ci_finished_at=ci_finished_at, superseded_by=superseded_by,
                      created_at=created, updated_at=created)
    with platform.transaction() as tx:
        tx.apply(UnitOfWork(runs=[(run, None)]))
    return run


def _report(platform, application, **kwargs):
    kwargs.setdefault("price_per_runner_hour", None)
    kwargs.setdefault("currency", None)
    return ci_cost(platform, {application.id}, now=NOW, days=kwargs.pop("days", 30), **kwargs)


def test_ci_seconds_are_dispatch_to_leaving_ci_and_stage_seconds_are_kept_apart():
    platform = _platform()
    application = _app(platform)
    built = _run(platform, application, status=PipelineStatus.SUCCEEDED, held=46, digest=DIGEST)
    platform.record_stage_event(built.id, stage_id="build", stage_name="Build", status="succeeded", duration_ms=17_000)
    platform.record_stage_event(built.id, stage_id="checkout", stage_name="Checkout", status="succeeded")
    _run(platform, application, status=PipelineStatus.RUNNING, finished=False)   # still in CI: not counted

    row = _report(platform, application)["applications"][0]
    # The lab's case: 17 s of stages in a 46 s build. The stage sum is not the cost.
    assert row["ciSeconds"] == 46.0 and row["stageSeconds"] == 17.0
    assert row["stagesWithoutDuration"] == 1 and row["runsWithoutCiTiming"] == 0
    assert row["cost"] is None


def test_a_finished_run_without_a_recorded_exit_is_counted_as_untimed_not_as_zero():
    platform = _platform()
    application = _app(platform)
    _run(platform, application, status=PipelineStatus.FAILED, held=None)   # predates ci_finished_at
    row = _report(platform, application)["applications"][0]
    assert row["ciSeconds"] == 0 and row["runsWithoutCiTiming"] == 1


def test_superseded_runs_split_by_reaching_ci_and_the_estimate_is_the_median_of_built_runs():
    platform = _platform()
    application = _app(platform)
    for held in (100, 200, 300):
        _run(platform, application, status=PipelineStatus.SUCCEEDED, held=held, digest=DIGEST)
    # A build whose deployment then failed still built its artifact: it counts.
    _run(platform, application, status=PipelineStatus.FAILED, held=250, digest=DIGEST)
    newer = uuid.uuid4()
    _run(platform, application, status=PipelineStatus.CANCELLED, admitted=False, superseded_by=newer)
    _run(platform, application, status=PipelineStatus.CANCELLED, admitted=False, superseded_by=newer)
    _run(platform, application, status=PipelineStatus.CANCELLED, held=10, superseded_by=newer)

    row = _report(platform, application)["applications"][0]
    assert (row["supersededBeforeAdmission"], row["supersededWhileBuilding"]) == (2, 1)
    assert row["estimatedAvoidedRunnerSeconds"] == 2 * 225.0  # median of 100, 200, 250, 300


def test_no_built_run_means_no_estimate_and_the_total_says_it_is_incomplete():
    platform = _platform()
    application = _app(platform)
    _run(platform, application, status=PipelineStatus.CANCELLED, admitted=False, superseded_by=uuid.uuid4())
    report = _report(platform, application)
    assert report["applications"][0]["estimatedAvoidedRunnerSeconds"] is None
    assert report["total"]["estimatedAvoidedRunnerSeconds"] is None
    assert report["total"]["estimatedAvoidedIncomplete"] is True


def test_cost_is_money_only_with_a_price_and_is_rounded_half_up():
    platform = _platform()
    application = _app(platform)
    _run(platform, application, status=PipelineStatus.SUCCEEDED, held=5400, digest=DIGEST)   # 1.5 h
    report = _report(platform, application, price_per_runner_hour=Decimal("0.333"), currency="EUR")
    # Nothing was superseded, and there is a built run to estimate from: the estimate is 0.
    assert report["applications"][0]["cost"] == {"currency": "EUR", "measured": "0.50", "estimatedAvoided": "0.00"}


def test_runs_outside_the_window_are_not_counted():
    platform = _platform()
    application = _app(platform)
    _run(platform, application, status=PipelineStatus.FAILED, held=30, created=T0 - timedelta(days=40))
    report = _report(platform, application)
    assert report["applications"][0]["runs"] == 0 and report["total"]["ciSeconds"] == 0


def test_leaving_ci_is_stamped_once_at_the_first_move_out_of_queued_or_running():
    platform = _platform()
    application = _app(platform)
    run = platform.start_pipeline(application.id, commit_sha="a" * 40, branch="main", environment=Environment.PROD,
                                  parameters={}, correlation_id="c", idempotency_key=uuid.uuid4().hex)
    platform.record_ci_result(run.id, PipelineStatus.RUNNING.value, None, [])
    assert platform.get_pipeline(run.id).ci_finished_at is None
    platform.record_ci_result(run.id, PipelineStatus.SUCCEEDED.value, DIGEST, [])
    waiting = platform.get_pipeline(run.id)
    assert waiting.status == PipelineStatus.WAITING_APPROVAL and waiting.ci_finished_at is not None
    # A later write from a stale copy does not move it.
    stale = replace(waiting, ci_finished_at=None, version=waiting.version + 1)
    with platform.transaction() as tx:
        tx.apply(UnitOfWork(runs=[(stale, waiting.version)]))
    assert platform.get_pipeline(run.id).ci_finished_at == waiting.ci_finished_at


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
