import os
import uuid

import pytest
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from app.workflows.activities import DeliveryActivities
from app.workflows.provision_and_deploy import (
    DeliveryInput,
    PreviewInput,
    PreviewWorkflow,
    ProvisionAndDeployWorkflow,
    RollbackWorkflow,
)


#: These run by default. They were opt-in behind NETCI_RUN_TEMPORAL_TEST=1, which meant
#: the delivery workflow -- the thing that actually drives a deployment -- had no executed
#: test on any host where nobody remembered to set it, and a skip reads exactly like a
#: pass in the summary line. The opt-out is for a host that cannot fetch Temporal's
#: time-skipping test server; on such a host the run says so rather than going quiet.
needs_temporal_test_server = pytest.mark.skipif(
    os.getenv("NETCI_SKIP_TEMPORAL_TEST") == "1",
    reason="NETCI_SKIP_TEMPORAL_TEST=1: Temporal time-skipping test server not available here",
)


class InMemoryEvidenceStore:
    def __init__(self, digest: str) -> None:
        self.digest = digest

    def load(self, pipeline_run_id: str) -> dict[str, object]:
        return {
            "applicationId": "app-1",
            "artifactDigest": self.digest,
            "sbom": {"generatedBy": "syft", "location": "memory://sbom"},
            "vulnerabilityScan": {"scanner": "trivy", "status": "passed", "critical": 0, "high": 0},
            "signature": {"provider": "cosign", "verified": True},
            "decision": "allow",
        }


class HealthyRuntimeRunner:
    async def deploy(self, delivery: DeliveryInput) -> str:
        return "deployment-1"

    async def health_check(self, delivery: DeliveryInput) -> bool:
        return True

    async def rollback(self, delivery: DeliveryInput) -> None:
        raise AssertionError("healthy delivery must not roll back")


class RecordingDeploymentReporter:
    def __init__(self) -> None:
        self.results = []
        self.rollbacks = []
        self.previews = []

    async def report(self, result) -> None:
        self.results.append(result)

    async def report_rollback(self, result) -> None:
        self.rollbacks.append(result)

    async def report_preview(self, result) -> None:
        self.previews.append(result)


@needs_temporal_test_server
@pytest.mark.asyncio
async def test_temporal_delivery_reaches_healthy_without_production_approval():
    digest = "sha256:" + "c" * 64
    delivery = DeliveryInput(
        application_id="app-1",
        pipeline_run_id="run-1",
        runtime="docker",
        environment="dev",
        artifact_digest=digest,
        deployment_id="deployment-core-1",
        parameters={"health_url": "http://example.invalid/healthz"},
    )
    reporter = RecordingDeploymentReporter()
    activities = DeliveryActivities(
        InMemoryEvidenceStore(digest),
        HealthyRuntimeRunner(),
        deployment_reporter=reporter,
    )

    async with await WorkflowEnvironment.start_time_skipping() as environment:
        task_queue = f"netci-test-{uuid.uuid4()}"
        async with Worker(
            environment.client,
            task_queue=task_queue,
            workflows=[ProvisionAndDeployWorkflow],
            activities=[
                activities.validate_artifact,
                activities.deploy,
                activities.health_check,
                activities.rollback,
                activities.report_deployment_result,
            ],
        ):
            result = await environment.client.execute_workflow(
                ProvisionAndDeployWorkflow.run,
                delivery,
                id=f"workflow-{uuid.uuid4()}",
                task_queue=task_queue,
            )

    assert result.status == "healthy"
    assert result.deployment_id == "deployment-core-1"
    assert result.artifact_digest == digest
    assert [(item.deployment_id, item.status) for item in reporter.results] == [
        ("deployment-core-1", "healthy")
    ]


class RollbackRuntimeRunner:
    """Executes the rollback for real (records it) and answers the health probe."""

    def __init__(self, healthy_after: bool = True) -> None:
        self.healthy_after = healthy_after
        self.rolled_back: list[str] = []

    async def deploy(self, delivery: DeliveryInput) -> str:
        raise AssertionError("a rollback must not run the deploy action")

    async def health_check(self, delivery: DeliveryInput) -> bool:
        return self.healthy_after

    async def rollback(self, delivery: DeliveryInput) -> None:
        self.rolled_back.append(delivery.artifact_digest)


@needs_temporal_test_server
@pytest.mark.asyncio
@pytest.mark.parametrize("healthy_after", [True, False])
async def test_rollback_workflow_executes_the_rollback_and_reports_what_the_health_check_said(healthy_after):
    """A manual rollback used to write a state and start nothing. This is the workflow
    that now runs it: verify the target, run the playbook, probe health, report -- with
    the fencing token and the per-deployment callback token the API handed over."""
    older = "sha256:" + "b" * 64
    delivery = DeliveryInput(
        application_id="app-1",
        pipeline_run_id="run-older",
        runtime="docker",
        environment="dev",
        artifact_digest=older,
        deployment_id="deployment-rb-1",
        parameters={"health_url": "http://example.invalid/healthz", "fencing_token": 7, "callback_token": "t.o.k"},
    )
    reporter = RecordingDeploymentReporter()
    runner = RollbackRuntimeRunner(healthy_after=healthy_after)
    activities = DeliveryActivities(InMemoryEvidenceStore(older), runner, deployment_reporter=reporter)

    async with await WorkflowEnvironment.start_time_skipping() as environment:
        task_queue = f"netci-test-{uuid.uuid4()}"
        async with Worker(
            environment.client,
            task_queue=task_queue,
            workflows=[RollbackWorkflow],
            activities=[
                activities.validate_artifact,
                activities.rollback,
                activities.health_check,
                activities.report_rollback_result,
            ],
        ):
            result = await environment.client.execute_workflow(
                RollbackWorkflow.run, delivery, id=f"rollback-{uuid.uuid4()}", task_queue=task_queue,
            )

    assert runner.rolled_back == [older]
    assert result.succeeded is healthy_after
    assert result.fencing_token == 7 and result.callback_token == "t.o.k"
    assert [(r.deployment_id, r.succeeded) for r in reporter.rollbacks] == [("deployment-rb-1", healthy_after)]
    assert reporter.results == [], "a rollback reports on /rollback-result, never as a deployment result"


@needs_temporal_test_server
@pytest.mark.asyncio
async def test_rollback_workflow_refuses_an_unverifiable_target_and_reports_the_reason():
    """A rollback is a deployment of an older digest; the signature gate applies to it."""
    older = "sha256:" + "b" * 64
    delivery = DeliveryInput(
        application_id="app-1", pipeline_run_id="run-older", runtime="docker", environment="dev",
        artifact_digest=older, deployment_id="deployment-rb-2", parameters={"fencing_token": 3},
    )
    reporter = RecordingDeploymentReporter()
    runner = RollbackRuntimeRunner()
    # Evidence for a *different* digest than the one being rolled back to.
    activities = DeliveryActivities(InMemoryEvidenceStore("sha256:" + "c" * 64), runner, deployment_reporter=reporter)

    async with await WorkflowEnvironment.start_time_skipping() as environment:
        task_queue = f"netci-test-{uuid.uuid4()}"
        async with Worker(
            environment.client, task_queue=task_queue, workflows=[RollbackWorkflow],
            activities=[activities.validate_artifact, activities.rollback, activities.health_check, activities.report_rollback_result],
        ):
            with pytest.raises(Exception):
                await environment.client.execute_workflow(
                    RollbackWorkflow.run, delivery, id=f"rollback-{uuid.uuid4()}", task_queue=task_queue,
                )

    assert runner.rolled_back == [], "nothing ran on the target"
    assert len(reporter.rollbacks) == 1
    failed = reporter.rollbacks[0]
    assert failed.succeeded is False
    assert failed.message.startswith("activity validate_artifact failed: PolicyViolation")


class PreviewRuntimeRunner:
    """Fakes the Ansible adapter: no process runs, no cluster is touched."""

    def __init__(self, url: str | None = None, fail: bool = False) -> None:
        self.url = url
        self.fail = fail
        self.calls: list[str] = []

    async def deploy(self, delivery: DeliveryInput) -> str:
        raise AssertionError("a preview must not run the deploy action")

    async def health_check(self, delivery: DeliveryInput) -> bool:
        raise AssertionError("a preview has no health check of its own")

    async def rollback(self, delivery: DeliveryInput) -> None:
        raise AssertionError("a preview must never roll back")

    async def preview(self, preview: PreviewInput) -> str | None:
        self.calls.append(preview.action)
        if self.fail:
            raise RuntimeError("preview-kubernetes.yml failed")
        return self.url


@needs_temporal_test_server
@pytest.mark.asyncio
async def test_preview_workflow_deploy_success_is_reported_active_with_the_url():
    digest = "sha256:" + "d" * 64
    preview = PreviewInput(
        application_id="app-1", pipeline_run_id="run-pr-1", preview_id="checkout-pr-1",
        action="deploy", namespace="preview-checkout-pr-1", release="checkout-pr-1",
        artifact_digest=digest, parameters={"callback_token": "t.o.k"},
    )
    reporter = RecordingDeploymentReporter()
    runner = PreviewRuntimeRunner(url="https://checkout-pr-1.preview.local")
    activities = DeliveryActivities(InMemoryEvidenceStore(digest), runner, deployment_reporter=reporter)

    async with await WorkflowEnvironment.start_time_skipping() as environment:
        task_queue = f"netci-test-{uuid.uuid4()}"
        async with Worker(
            environment.client,
            task_queue=task_queue,
            workflows=[PreviewWorkflow],
            activities=[activities.validate_artifact, activities.preview, activities.report_preview_result],
        ):
            result = await environment.client.execute_workflow(
                PreviewWorkflow.run, preview, id=f"preview-{uuid.uuid4()}", task_queue=task_queue,
            )

    assert runner.calls == ["deploy"]
    assert result.status == "active"
    assert result.url == "https://checkout-pr-1.preview.local"
    assert result.callback_token == "t.o.k"
    assert [(r.preview_id, r.status, r.url) for r in reporter.previews] == [
        ("checkout-pr-1", "active", "https://checkout-pr-1.preview.local")
    ]


@needs_temporal_test_server
@pytest.mark.asyncio
async def test_preview_workflow_a_runner_failure_is_reported_failed_not_raised():
    """Unlike ProvisionAndDeployWorkflow, a preview has nothing under it to roll back to
    -- a failure is reported and the workflow still completes."""

    digest = "sha256:" + "d" * 64
    preview = PreviewInput(
        application_id="app-1", pipeline_run_id="run-pr-2", preview_id="checkout-pr-2",
        action="deploy", namespace="preview-checkout-pr-2", release="checkout-pr-2",
        artifact_digest=digest,
    )
    reporter = RecordingDeploymentReporter()
    runner = PreviewRuntimeRunner(fail=True)
    activities = DeliveryActivities(InMemoryEvidenceStore(digest), runner, deployment_reporter=reporter)

    async with await WorkflowEnvironment.start_time_skipping() as environment:
        task_queue = f"netci-test-{uuid.uuid4()}"
        async with Worker(
            environment.client,
            task_queue=task_queue,
            workflows=[PreviewWorkflow],
            activities=[activities.validate_artifact, activities.preview, activities.report_preview_result],
        ):
            result = await environment.client.execute_workflow(
                PreviewWorkflow.run, preview, id=f"preview-{uuid.uuid4()}", task_queue=task_queue,
            )

    assert result.status == "failed"
    assert "preview-kubernetes.yml failed" in result.message
    assert len(reporter.previews) == 1 and reporter.previews[0].status == "failed"


@needs_temporal_test_server
@pytest.mark.asyncio
async def test_preview_workflow_teardown_never_validates_the_artifact_and_reports_destroyed():
    preview = PreviewInput(
        application_id="app-1", pipeline_run_id="run-pr-3", preview_id="checkout-pr-3",
        action="teardown", namespace="preview-checkout-pr-3", release="checkout-pr-3",
    )
    reporter = RecordingDeploymentReporter()
    runner = PreviewRuntimeRunner()

    class RefusingEvidenceStore:
        def load(self, pipeline_run_id: str) -> dict[str, object]:
            raise AssertionError("teardown must not read evidence: there is no artifact to verify")

    activities = DeliveryActivities(RefusingEvidenceStore(), runner, deployment_reporter=reporter)

    async with await WorkflowEnvironment.start_time_skipping() as environment:
        task_queue = f"netci-test-{uuid.uuid4()}"
        async with Worker(
            environment.client,
            task_queue=task_queue,
            workflows=[PreviewWorkflow],
            activities=[activities.validate_artifact, activities.preview, activities.report_preview_result],
        ):
            result = await environment.client.execute_workflow(
                PreviewWorkflow.run, preview, id=f"preview-{uuid.uuid4()}", task_queue=task_queue,
            )

    assert runner.calls == ["teardown"]
    assert result.status == "destroyed"
