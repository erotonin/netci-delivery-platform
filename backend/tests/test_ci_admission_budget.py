"""Admission also waits for Jenkins itself (ADR-050): a saturated or unreachable CI takes
nothing more, and the runs keep waiting in netCI instead of failing at launch."""

from __future__ import annotations

import uuid

from app.adapters.ci_launcher import JenkinsCiLauncher, LaunchedCi
from app.adapters.jenkins_router import ControllerState, JenkinsController, JenkinsRouter
from app.delivery import DeliveryPlatform
from app.domain.models import Environment, PipelineStatus, Runtime
from app.store.memory import InMemoryDatabase


class BudgetLauncher:
    mode = "budget"

    def __init__(self, budget):
        self.budget = budget
        self.launched = []

    def admission_budget(self):
        if isinstance(self.budget, Exception):
            raise self.budget
        return self.budget

    def launch(self, request):
        self.launched.append(request.pipeline_run_id)
        return LaunchedCi(controller_id="jenkins-a", external_run_id=f"job#{len(self.launched)}")

    def abort(self, jenkins_run_id):
        return None

    def get_status(self, jenkins_run_id):
        return None


def _start_three(launcher):
    platform = DeliveryPlatform(database=InMemoryDatabase(), ci_launcher=launcher)
    application = platform.create_application(
        name=f"b-{uuid.uuid4().hex[:6]}", repository_url="https://git.example/b",
        pipeline_template="container-ci-cd-v1", runtime=Runtime.DOCKER, default_environment=Environment.DEV,
        stages=[], idempotency_key=uuid.uuid4().hex)
    runs = [platform.start_pipeline(application.id, commit_sha=f"{i}" * 40, branch="main",
                                    environment=Environment.DEV, parameters={}, correlation_id="c",
                                    idempotency_key=uuid.uuid4().hex) for i in range(3)]
    return platform, runs


def test_a_saturated_ci_admits_nothing_and_the_runs_wait_instead_of_failing():
    launcher = BudgetLauncher(0)
    platform, runs = _start_three(launcher)
    assert launcher.launched == []
    assert all(platform.get_pipeline(r.id).status == PipelineStatus.QUEUED
               and platform.get_pipeline(r.id).admitted_at is None for r in runs)

    launcher.budget = 2
    platform.admit_waiting_runs()
    assert launcher.launched == [runs[0].id, runs[1].id], "oldest first, and no more than CI can take"


def test_a_failing_capacity_probe_is_read_as_take_nothing():
    launcher = BudgetLauncher(RuntimeError("controller timed out"))
    platform, runs = _start_three(launcher)
    assert launcher.launched == [] and platform.get_pipeline(runs[0].id).admitted_at is None


class Adapter:
    def __init__(self, queued, healthy=True):
        self._queued, self._healthy = queued, healthy

    def health_check(self):
        return self._healthy

    def queued_builds(self):
        return self._queued


def _jenkins(adapters, limit=2):
    controllers = [JenkinsController(controller_id=name, executors_total=4) for name in adapters]
    return JenkinsCiLauncher(JenkinsRouter(controllers), adapters, max_queued_per_controller=limit)


def test_the_budget_is_what_each_healthy_controller_has_room_for_in_its_own_queue():
    assert _jenkins({"a": Adapter(0), "b": Adapter(1)}).admission_budget() == 3
    assert _jenkins({"a": Adapter(5)}).admission_budget() == 0


def test_a_down_controller_or_an_unreadable_queue_contributes_nothing():
    launcher = _jenkins({"a": Adapter(0, healthy=False), "b": Adapter(None)})
    assert launcher.admission_budget() == 0
    assert launcher.router.controllers[0].state == ControllerState.UNAVAILABLE


def test_the_http_adapter_reports_an_unreachable_queue_as_unknown_not_empty(monkeypatch):
    from app.adapters.jenkins_http import JenkinsHttpAdapter, JenkinsHttpError

    adapter = JenkinsHttpAdapter.__new__(JenkinsHttpAdapter)

    def refuse(*args, **kwargs):
        raise JenkinsHttpError(0, "connection refused")

    monkeypatch.setattr(adapter, "_request", refuse, raising=False)
    assert adapter.queued_builds() is None
    monkeypatch.setattr(adapter, "_request", lambda *a, **k: (200, {}, b'{"items": [{"id": 1}, {"id": 2}]}'),
                        raising=False)
    assert adapter.queued_builds() == 2
