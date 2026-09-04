"""Tests for the CI/CD seams, the delivery-event outbox and concurrency control.

These cover the wiring that used to stop at "the code exists": that starting a
pipeline really reaches a CI engine, that a successful build really starts the
durable CD workflow, and that every DORA number has a source event behind it.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest

from app.adapters.cd_orchestrator import CdStartError, CdStartRequest
from app.adapters.ci_launcher import (
    CiLaunchError,
    CiLaunchRequest,
    JenkinsCiLauncher,
    LaunchedCi,
    NullCiLauncher,
)
from app.adapters.interfaces import JenkinsRun
from app.adapters.jenkins_router import ControllerState, JenkinsController, JenkinsRouter
from app.delivery import DeliveryError, DeliveryPlatform
from app.persistence import UnitOfWork
from app.domain.models import DeliveryEventType, DeploymentStatus, Environment, PipelineStatus, Runtime

DIGEST = "sha256:" + "a" * 64
OTHER_DIGEST = "sha256:" + "b" * 64


class RecordingCiLauncher:
    """A CI engine stand-in that records what the domain handed it."""

    mode = "recording"

    def __init__(self, *, fail: bool = False) -> None:
        self.requests: list[CiLaunchRequest] = []
        self.fail = fail

    def launch(self, request: CiLaunchRequest) -> LaunchedCi:
        self.requests.append(request)
        if self.fail:
            raise CiLaunchError("no healthy Jenkins controller with capacity")
        return LaunchedCi(controller_id="jenkins-a", external_run_id="netci-job#7", console_url="http://jenkins/7")


class RecordingCdOrchestrator:
    mode = "recording"

    def __init__(self, *, fail: bool = False) -> None:
        self.started: list[CdStartRequest] = []
        self.signals: list[tuple[str, str, str]] = []
        self.fail = fail

    def start(self, request: CdStartRequest) -> str:
        if self.fail:
            raise CdStartError("temporal unreachable")
        self.started.append(request)
        return request.workflow_id

    def signal_approval(self, workflow_id: str, actor: str, comment: str) -> None:
        self.signals.append((workflow_id, actor, comment))


def build_platform(launcher=None, orchestrator=None) -> DeliveryPlatform:
    return DeliveryPlatform(launcher or NullCiLauncher(), orchestrator or RecordingCdOrchestrator())


def create_application(platform: DeliveryPlatform, name: str = "hello-netci"):
    return platform.create_application(
        name=name,
        repository_url="https://github.com/example/hello-netci",
        pipeline_template="container-ci-cd-v1",
        runtime=Runtime.DOCKER,
        default_environment=Environment.DEV,
        stages=[],
        idempotency_key=None,
    )


def start_run(platform: DeliveryPlatform, application, environment=Environment.DEV, commit="abc1234", key=None):
    return platform.start_pipeline(
        application.id,
        commit_sha=commit,
        branch="main",
        environment=environment,
        parameters={},
        correlation_id="correlation-1",
        idempotency_key=key,
    )


def drive_to_deployment(platform: DeliveryPlatform, run, environment=Environment.PROD):
    platform.record_ci_result(run.id, PipelineStatus.RUNNING.value, None, [])
    result = platform.record_ci_result(run.id, PipelineStatus.SUCCEEDED.value, DIGEST, ["built"])
    assert result.deployment is not None
    return result.deployment


def record_allowed_evidence(platform: DeliveryPlatform, run) -> None:
    platform.record_security_evidence(
        run.id,
        {
            "artifactDigest": DIGEST,
            "sbom": {"generatedBy": "syft", "location": "s3://evidence/sbom.json"},
            "vulnerabilityScan": {"scanner": "trivy", "status": "passed", "critical": 0, "high": 0},
            "signature": {"provider": "cosign", "verified": True},
        },
    )


# --------------------------------------------------------------------- CI seam


def test_verified_release_artifact_becomes_a_real_scheduled_production_deployment():
    orchestrator = RecordingCdOrchestrator()
    platform = build_platform(orchestrator=orchestrator)
    application = create_application(platform)
    source = start_run(platform, application, Environment.STAGING)
    platform.record_ci_result(source.id, PipelineStatus.RUNNING.value, None, [])
    record_allowed_evidence(platform, source)
    source_deployment = platform.record_ci_result(
        source.id, PipelineStatus.SUCCEEDED.value, DIGEST, ["built"]
    ).deployment
    assert source_deployment is not None
    platform.record_deployment_result(source_deployment.id, DeploymentStatus.HEALTHY.value, "ok")
    scheduled_for = datetime.now(timezone.utc) + timedelta(hours=2)

    production = platform.create_production_promotion(
        source.id,
        requested_by="developer-1",
        correlation_id="production-request:one",
        production_request_id="one",
        scheduled_for=scheduled_for,
        rollback_strategy="manual",
        run_automation_tests=False,
    )
    approved = platform.approve_deployment(production.id, "reviewer-1")

    assert approved.status == DeploymentStatus.DEPLOYING
    assert approved.artifact_digest == DIGEST
    request = orchestrator.started[-1]
    assert request.deployment_id == production.id
    assert request.environment == "prod"
    assert request.parameters["notBefore"] == scheduled_for.isoformat()
    assert request.parameters["rollbackStrategy"] == "manual"
    assert request.parameters["runAutomationTests"] is False
    assert platform.deployment_requested_by(production.id) == "developer-1"


def test_retrying_the_same_production_request_reuses_its_run_and_deployment():
    platform = build_platform()
    application = create_application(platform)
    source = start_run(platform, application, Environment.STAGING)
    platform.record_ci_result(source.id, PipelineStatus.RUNNING.value, None, [])
    record_allowed_evidence(platform, source)
    source_deployment = platform.record_ci_result(
        source.id, PipelineStatus.SUCCEEDED.value, DIGEST, ["built"]
    ).deployment
    assert source_deployment is not None
    # A run is only promotable once its deployment is healthy: CI succeeding is not the
    # same fact as the release being live.
    platform.record_deployment_result(source_deployment.id, DeploymentStatus.HEALTHY.value, "ok")
    scheduled_for = datetime.now(timezone.utc) + timedelta(hours=2)
    arguments = {
        "requested_by": "developer-1",
        "correlation_id": "production-request:retry-safe",
        "production_request_id": "retry-safe",
        "scheduled_for": scheduled_for,
    }

    first = platform.create_production_promotion(source.id, **arguments)
    replay = platform.create_production_promotion(source.id, **arguments)

    assert replay.id == first.id
    assert len(platform.list_deployments(application.id)) == 2  # staging + one production
    assert len(platform.list_pipeline_runs(application.id)) == 2  # source + one promotion


def test_reusing_a_production_request_id_with_different_inputs_is_rejected():
    platform = build_platform()
    application = create_application(platform)
    source = start_run(platform, application, Environment.STAGING)
    platform.record_ci_result(source.id, PipelineStatus.RUNNING.value, None, [])
    record_allowed_evidence(platform, source)
    source_deployment = platform.record_ci_result(
        source.id, PipelineStatus.SUCCEEDED.value, DIGEST, ["built"]
    ).deployment
    assert source_deployment is not None
    platform.record_deployment_result(source_deployment.id, DeploymentStatus.HEALTHY.value, "ok")
    scheduled_for = datetime.now(timezone.utc) + timedelta(hours=2)

    platform.create_production_promotion(
        source.id,
        requested_by="developer-1",
        correlation_id="production-request:conflict",
        production_request_id="conflict",
        scheduled_for=scheduled_for,
    )
    with pytest.raises(DeliveryError) as failure:
        platform.create_production_promotion(
            source.id,
            requested_by="someone-else",
            correlation_id="production-request:conflict",
            production_request_id="conflict",
            scheduled_for=scheduled_for,
        )

    assert failure.value.code == "IDEMPOTENCY_KEY_REUSED"


def test_starting_a_pipeline_dispatches_to_the_ci_engine_and_records_its_identity():
    launcher = RecordingCiLauncher()
    platform = build_platform(launcher)
    application = create_application(platform)

    run = start_run(platform, application)

    assert len(launcher.requests) == 1
    dispatched = launcher.requests[0]
    assert dispatched.pipeline_run_id == run.id
    assert dispatched.commit_sha == "abc1234"
    assert dispatched.repository_url == application.repository_url
    assert dispatched.stages == application.stages
    assert run.jenkins_run_id == "jenkins-a:netci-job#7"
    _, lines = platform.get_pipeline_logs(run.id)
    assert any("ci-dispatched controller=jenkins-a" in line for line in lines)


def test_a_ci_engine_that_refuses_the_build_fails_the_run_instead_of_leaving_it_queued():
    launcher = RecordingCiLauncher(fail=True)
    platform = build_platform(launcher)
    application = create_application(platform)

    with pytest.raises(DeliveryError) as failure:
        start_run(platform, application)

    assert failure.value.code == "CI_LAUNCH_FAILED"
    assert failure.value.status_code == 502
    run = platform.list_pipeline_runs(application.id)[0]
    assert run.status == PipelineStatus.FAILED
    _, lines = platform.get_pipeline_logs(run.id)
    assert any("ci-launch-failed" in line for line in lines)


def test_the_default_launcher_leaves_the_run_queued_for_an_external_callback():
    platform = build_platform(NullCiLauncher())
    application = create_application(platform)

    run = start_run(platform, application)

    assert run.status == PipelineStatus.QUEUED
    assert run.jenkins_run_id is None


# ----------------------------------------------------------------- CD seam


def test_successful_non_production_ci_starts_the_durable_workflow():
    orchestrator = RecordingCdOrchestrator()
    platform = build_platform(orchestrator=orchestrator)
    application = create_application(platform)
    run = start_run(platform, application, Environment.STAGING)

    deployment = drive_to_deployment(platform, run)

    assert deployment.status == DeploymentStatus.DEPLOYING
    assert len(orchestrator.started) == 1
    started = orchestrator.started[0]
    assert started.deployment_id == deployment.id
    assert started.artifact_digest == DIGEST
    assert started.require_approval is False
    assert started.workflow_id == f"netci-deploy-{deployment.id}"
    assert platform.get_pipeline(run.id).workflow_id == started.workflow_id


def test_production_waits_for_approval_before_any_workflow_starts():
    orchestrator = RecordingCdOrchestrator()
    platform = build_platform(orchestrator=orchestrator)
    application = create_application(platform)
    run = start_run(platform, application, Environment.PROD)

    deployment = drive_to_deployment(platform, run)

    assert deployment.status == DeploymentStatus.PENDING_APPROVAL
    assert orchestrator.started == []
    assert platform.get_pipeline(run.id).workflow_id is None

    platform.approve_deployment(deployment.id, "reviewer-1")

    assert len(orchestrator.started) == 1
    assert orchestrator.started[0].deployment_id == deployment.id


def test_approval_signals_a_workflow_that_is_already_waiting():
    orchestrator = RecordingCdOrchestrator()
    platform = build_platform(orchestrator=orchestrator)
    application = create_application(platform)
    run = start_run(platform, application, Environment.PROD)
    deployment = drive_to_deployment(platform, run)
    # Simulate a workflow that was started ahead of the approval.
    with platform.database.transaction() as transaction:
        current = transaction.pipeline_run(run.id)
        transaction.apply(
            UnitOfWork(
                runs=[
                    (
                        replace(
                            current,
                            workflow_id="netci-deploy-existing",
                            version=current.version + 1,
                        ),
                        current.version,
                    )
                ]
            )
        )

    platform.approve_deployment(deployment.id, "reviewer-2")

    assert orchestrator.signals == [("netci-deploy-existing", "reviewer-2", "approved via netCI")]
    assert orchestrator.started == []


def test_a_workflow_engine_that_is_down_fails_the_deployment_rather_than_reporting_success():
    platform = build_platform(orchestrator=RecordingCdOrchestrator(fail=True))
    application = create_application(platform)
    run = start_run(platform, application, Environment.STAGING)
    platform.record_ci_result(run.id, PipelineStatus.RUNNING.value, None, [])

    with pytest.raises(DeliveryError) as failure:
        platform.record_ci_result(run.id, PipelineStatus.SUCCEEDED.value, DIGEST, [])

    assert failure.value.code == "CD_START_FAILED"
    assert failure.value.status_code == 502
    assert platform.list_deployments(application.id)[0].status == DeploymentStatus.FAILED
    assert platform.get_pipeline(run.id).status == PipelineStatus.FAILED


# ------------------------------------------------------------- source events


def test_starting_a_pipeline_records_the_commit_event_lead_time_is_measured_from():
    platform = build_platform()
    application = create_application(platform)

    run = start_run(platform, application)

    events = platform.delivery_events(application.id)
    assert [event.event_type for event in events] == [DeliveryEventType.COMMIT]
    assert events[0].commit_sha == "abc1234"
    assert events[0].pipeline_run_id == run.id


def test_a_supplied_commit_timestamp_is_used_instead_of_the_queue_time():
    platform = build_platform()
    application = create_application(platform)
    authored_at = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)

    platform.start_pipeline(
        application.id,
        commit_sha="abc1234",
        branch="main",
        environment=Environment.PROD,
        parameters={"commitTimestamp": authored_at.isoformat()},
        correlation_id="correlation-1",
        idempotency_key=None,
    )

    assert platform.delivery_events(application.id)[0].occurred_at == authored_at


def test_only_production_deployments_produce_deployment_events():
    platform = build_platform()
    application = create_application(platform)
    run = start_run(platform, application, Environment.STAGING)
    deployment = drive_to_deployment(platform, run)

    platform.record_deployment_result(deployment.id, DeploymentStatus.HEALTHY.value, "ok")

    types = [event.event_type for event in platform.delivery_events(application.id)]
    assert types == [DeliveryEventType.COMMIT]


def test_retried_terminal_deployment_callback_is_idempotent():
    platform = build_platform()
    application = create_application(platform)
    run = start_run(platform, application, Environment.STAGING)
    deployment = drive_to_deployment(platform, run)

    first = platform.record_deployment_result(deployment.id, DeploymentStatus.HEALTHY.value, "ok")
    replay = platform.record_deployment_result(deployment.id, DeploymentStatus.HEALTHY.value, "retry")

    assert replay == first
    assert len([record for record in platform.audit_records() if record.event_type == "deployment.healthy"]) == 1


def test_a_failed_production_deployment_and_its_later_recovery_are_linked_by_deployment_id():
    platform = build_platform()
    application = create_application(platform)
    first = start_run(platform, application, Environment.PROD, commit="aaa1111")
    failed_deployment = drive_to_deployment(platform, first)
    platform.approve_deployment(failed_deployment.id, "reviewer")
    platform.record_deployment_result(failed_deployment.id, DeploymentStatus.FAILED.value, "boom")

    second = start_run(platform, application, Environment.PROD, commit="bbb2222")
    fixed_deployment = drive_to_deployment(platform, second)
    platform.approve_deployment(fixed_deployment.id, "reviewer")
    platform.record_deployment_result(fixed_deployment.id, DeploymentStatus.HEALTHY.value, "ok")

    events = platform.delivery_events(application.id)
    failures = [item for item in events if item.event_type == DeliveryEventType.DEPLOYMENT and item.requires_intervention]
    recoveries = [item for item in events if item.event_type == DeliveryEventType.RECOVERY]
    assert len(failures) == 1
    assert len(recoveries) == 1
    assert recoveries[0].deployment_id == failed_deployment.id
    assert recoveries[0].occurred_at >= failures[0].occurred_at


def test_a_recovery_is_only_emitted_once_for_the_same_failure():
    platform = build_platform()
    application = create_application(platform)
    first = start_run(platform, application, Environment.PROD, commit="aaa1111")
    broken = drive_to_deployment(platform, first)
    platform.approve_deployment(broken.id, "reviewer")
    platform.record_deployment_result(broken.id, DeploymentStatus.FAILED.value, "boom")

    for index, commit in enumerate(("bbb2222", "ccc3333")):
        run = start_run(platform, application, Environment.PROD, commit=commit)
        deployment = drive_to_deployment(platform, run)
        platform.approve_deployment(deployment.id, "reviewer")
        platform.record_deployment_result(deployment.id, DeploymentStatus.HEALTHY.value, f"ok-{index}")

    recoveries = [item for item in platform.delivery_events(application.id) if item.event_type == DeliveryEventType.RECOVERY]
    assert len(recoveries) == 1


def test_rolling_back_a_healthy_production_release_is_recorded_as_a_change_failure():
    platform = build_platform()
    application = create_application(platform)
    run = start_run(platform, application, Environment.PROD)
    deployment = drive_to_deployment(platform, run)
    platform.approve_deployment(deployment.id, "reviewer")
    platform.record_deployment_result(deployment.id, DeploymentStatus.HEALTHY.value, "ok")

    platform.rollback_deployment(deployment.id, OTHER_DIGEST)

    failures = [
        item
        for item in platform.delivery_events(application.id)
        if item.event_type == DeliveryEventType.DEPLOYMENT and item.requires_intervention
    ]
    assert len(failures) == 1
    assert failures[0].deployment_id == deployment.id


def test_rolling_back_a_failed_production_deployment_records_the_restore():
    platform = build_platform()
    application = create_application(platform)
    run = start_run(platform, application, Environment.PROD)
    deployment = drive_to_deployment(platform, run)
    platform.approve_deployment(deployment.id, "reviewer")
    platform.record_deployment_result(deployment.id, DeploymentStatus.FAILED.value, "boom")

    platform.rollback_deployment(deployment.id, OTHER_DIGEST)

    recoveries = [item for item in platform.delivery_events(application.id) if item.event_type == DeliveryEventType.RECOVERY]
    assert len(recoveries) == 1
    assert recoveries[0].deployment_id == deployment.id


# ------------------------------------------------------------- concurrency


def test_a_second_callback_racing_the_first_is_rejected_rather_than_overwriting_it():
    platform = build_platform()
    application = create_application(platform)
    run = start_run(platform, application)
    stale = platform.get_pipeline(run.id)

    platform.record_ci_result(run.id, PipelineStatus.RUNNING.value, None, [])
    assert platform.get_pipeline(run.id).version == stale.version + 1

    # Replaying the queued->running callback must not move an already-running run.
    with pytest.raises(DeliveryError) as failure:
        platform.record_ci_result(run.id, PipelineStatus.RUNNING.value, None, [])
    assert failure.value.code == "INVALID_PIPELINE_STATE"


def test_every_state_change_bumps_the_record_version():
    platform = build_platform()
    application = create_application(platform)
    run = start_run(platform, application, Environment.PROD)
    platform.record_ci_result(run.id, PipelineStatus.RUNNING.value, None, [])
    deployment = platform.record_ci_result(run.id, PipelineStatus.SUCCEEDED.value, DIGEST, []).deployment
    assert deployment is not None

    approved = platform.approve_deployment(deployment.id, "reviewer")

    assert approved.version == deployment.version + 1
    assert platform.get_pipeline(run.id).version > run.version


# ------------------------------------------------------------- idempotency


def test_replaying_a_start_returns_the_run_in_its_current_state_not_the_first_snapshot():
    platform = build_platform()
    application = create_application(platform)
    first = start_run(platform, application, key="key-1")
    platform.record_ci_result(first.id, PipelineStatus.RUNNING.value, None, [])

    replayed = start_run(platform, application, key="key-1")

    assert replayed.id == first.id
    assert replayed.status == PipelineStatus.RUNNING


def test_the_same_key_with_a_different_payload_is_a_conflict():
    platform = build_platform()
    application = create_application(platform)
    start_run(platform, application, commit="abc1234", key="key-1")

    with pytest.raises(DeliveryError) as failure:
        start_run(platform, application, commit="zzz9999", key="key-1")

    assert failure.value.code == "IDEMPOTENCY_KEY_REUSED"


# ---------------------------------------------------------------- routing


class StubAdapter:
    def __init__(self, *, healthy: bool = True, trigger_error: Exception | None = None) -> None:
        self.healthy = healthy
        self.trigger_error = trigger_error
        self.triggered: list[str] = []

    def health_check(self) -> bool:
        return self.healthy

    def create_or_update_job(self, application_id: UUID, template_id: str) -> str:
        return f"netci-{application_id}"

    def trigger_ci_run(self, job_name: str, request: CiLaunchRequest) -> JenkinsRun:
        if self.trigger_error is not None:
            raise self.trigger_error
        self.triggered.append(job_name)
        return JenkinsRun(run_id=f"{job_name}#3", status="running")


def launch_request() -> CiLaunchRequest:
    from uuid import uuid4

    return CiLaunchRequest(
        application_id=uuid4(),
        application_name="hello-netci",
        repository_url="https://github.com/example/hello-netci",
        pipeline_template="container-ci-cd-v1",
        runtime="docker",
        stages=("checkout", "build"),
        pipeline_run_id=uuid4(),
        commit_sha="abc1234",
        branch="main",
        environment="dev",
        correlation_id="correlation-1",
        parameters={},
    )


def test_an_unreachable_controller_is_marked_unavailable_and_the_build_moves_to_its_peer():
    controllers = [JenkinsController("jenkins-a"), JenkinsController("jenkins-b")]
    adapters = {"jenkins-a": StubAdapter(healthy=False), "jenkins-b": StubAdapter()}
    launcher = JenkinsCiLauncher(JenkinsRouter(controllers), adapters)

    launched = launcher.launch(launch_request())

    assert launched.controller_id == "jenkins-b"
    assert controllers[0].state == ControllerState.UNAVAILABLE


def test_a_controller_that_fails_at_trigger_time_falls_through_to_the_next():
    controllers = [JenkinsController("jenkins-a"), JenkinsController("jenkins-b")]
    adapters = {
        "jenkins-a": StubAdapter(trigger_error=RuntimeError("connection reset")),
        "jenkins-b": StubAdapter(),
    }
    launcher = JenkinsCiLauncher(JenkinsRouter(controllers), adapters)

    launched = launcher.launch(launch_request())

    assert launched.controller_id == "jenkins-b"
    assert adapters["jenkins-b"].triggered  # type: ignore[union-attr]


def test_when_no_controller_accepts_the_build_the_launcher_says_which_it_tried():
    controllers = [JenkinsController("jenkins-a")]
    adapters = {"jenkins-a": StubAdapter(trigger_error=RuntimeError("boom"))}
    launcher = JenkinsCiLauncher(JenkinsRouter(controllers), adapters)

    with pytest.raises(CiLaunchError, match="jenkins-a"):
        launcher.launch(launch_request())


# ------------------------------------------------------------------- DORA


def test_the_portal_projects_dora_only_from_recorded_events():
    from app.portal import PortalService

    platform = build_platform()
    portal = PortalService(platform)
    application = create_application(platform, name="dora-app")

    empty = portal.dora_projection([application.id])
    assert empty["sourceEventCount"] == 0
    assert [metric["value"] for metric in empty["metrics"]] == [0, 0, 0, 0]  # type: ignore[index,union-attr]

    run = start_run(platform, application, Environment.PROD)
    deployment = drive_to_deployment(platform, run)
    platform.approve_deployment(deployment.id, "reviewer")
    platform.record_deployment_result(deployment.id, DeploymentStatus.HEALTHY.value, "ok")

    projected = portal.dora_projection([application.id])
    metrics = {metric["key"]: metric["value"] for metric in projected["metrics"]}  # type: ignore[index,union-attr]
    assert projected["sourceEventCount"] == 2  # one commit, one production deployment
    assert metrics["deploymentFrequency"] > 0
    assert metrics["changeFailureRate"] == 0


def test_events_older_than_the_reporting_window_are_excluded():
    from app.portal import DORA_WINDOW_DAYS, PortalService

    platform = build_platform()
    portal = PortalService(platform)
    application = create_application(platform, name="window-app")
    run = start_run(platform, application, Environment.PROD)
    deployment = drive_to_deployment(platform, run)
    platform.approve_deployment(deployment.id, "reviewer")
    platform.record_deployment_result(deployment.id, DeploymentStatus.HEALTHY.value, "ok")

    aged = datetime.now(timezone.utc) - timedelta(days=DORA_WINDOW_DAYS + 1)
    with platform.database.transaction() as transaction:
        # Age the recorded events in place. Backdating them through the store rather than
        # a process attribute is the point: the projection must read what was written.
        transaction._state.events = [  # noqa: SLF001 - ageing recorded events for the assertion
            replace(event, occurred_at=aged) for event in transaction._state.events
        ]

    assert portal.dora_projection([application.id])["sourceEventCount"] == 0
