"""A build sent through netci-queue gets its Jenkins page once the engine has one.

At submission no replica has numbered the build, so the run had no console link, nothing filled
it in later, and "Open Jenkins" stayed disabled for every queued build on the HA lab (ADR-091).
"""

from __future__ import annotations

import uuid

from app.adapters.ci_launcher import LaunchedCi
from app.delivery import DeliveryPlatform
from app.domain.models import Environment, PipelineStatus, Runtime
from app.store.memory import InMemoryDatabase


class QueueLauncher:
    mode = "recording"

    def __init__(self, page: str | None):
        self.page = page
        self.asked: list[str] = []

    def launch(self, request):
        return LaunchedCi(controller_id="jenkins-ha", external_run_id="queue:q-1")  # no page yet

    def abort(self, jenkins_run_id):
        return None

    def get_status(self, jenkins_run_id):
        return None

    def console_url(self, jenkins_run_id):
        self.asked.append(jenkins_run_id)
        return self.page


def _run(platform):
    app = platform.create_application(
        name=f"queue-{uuid.uuid4().hex[:6]}", repository_url="https://git.example/app",
        pipeline_template="container-ci-cd-v1", runtime=Runtime.DOCKER, default_environment=Environment.DEV,
        stages=[], idempotency_key=uuid.uuid4().hex,
    )
    run = platform.start_pipeline(
        app.id, commit_sha="a" * 40, branch="main", environment=Environment.DEV, parameters={},
        correlation_id="c", idempotency_key=uuid.uuid4().hex, trigger={"event": "manual"},
    )
    return platform.get_pipeline(run.id)


def test_the_page_is_asked_of_the_engine_when_the_build_starts():
    launcher = QueueLauncher("http://jenkins/job/netci-x/7/")
    platform = DeliveryPlatform(database=InMemoryDatabase(), ci_launcher=launcher)
    run = _run(platform)
    assert run.jenkins_run_id == "jenkins-ha:queue:q-1" and run.console_url is None
    platform.record_ci_result(run.id, result_status="running", artifact_digest=None, log_lines=[])
    after = platform.get_pipeline(run.id)
    assert after.status == PipelineStatus.RUNNING
    assert after.console_url == "http://jenkins/job/netci-x/7/"
    # Known now: later results do not ask again.
    platform.record_ci_result(run.id, result_status="failed", artifact_digest=None, log_lines=[])
    assert launcher.asked == ["jenkins-ha:queue:q-1"]


def test_an_engine_without_a_page_yet_leaves_the_run_as_it_was_and_is_asked_again():
    launcher = QueueLauncher(None)
    platform = DeliveryPlatform(database=InMemoryDatabase(), ci_launcher=launcher)
    run = _run(platform)
    platform.record_ci_result(run.id, result_status="running", artifact_digest=None, log_lines=[])
    assert platform.get_pipeline(run.id).console_url is None
    launcher.page = "http://jenkins/job/netci-x/7/"
    platform.record_ci_result(run.id, result_status="failed", artifact_digest=None, log_lines=[])
    after = platform.get_pipeline(run.id)
    assert after.status == PipelineStatus.FAILED and after.console_url == "http://jenkins/job/netci-x/7/"
