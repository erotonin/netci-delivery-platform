import os
import uuid

import pytest
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from app.workflows.activities import DeliveryActivities
from app.workflows.provision_and_deploy import DeliveryInput, ProvisionAndDeployWorkflow


class InMemoryEvidenceStore:
    def __init__(self, digest: str) -> None:
        self.digest = digest

    def load(self, pipeline_run_id: str) -> dict[str, object]:
        return {
            "applicationId": "app-1",
            "artifactDigest": self.digest,
            "sbom": {"location": "memory://sbom"},
            "vulnerabilityScan": {"status": "passed"},
            "signature": {"verified": True},
            "decision": "allow",
        }


class HealthyRuntimeRunner:
    async def deploy(self, delivery: DeliveryInput) -> str:
        return "deployment-1"

    async def health_check(self, delivery: DeliveryInput) -> bool:
        return True

    async def rollback(self, delivery: DeliveryInput) -> None:
        raise AssertionError("healthy delivery must not roll back")


@pytest.mark.skipif(
    os.getenv("NETCI_RUN_TEMPORAL_TEST") != "1",
    reason="Temporal time-skipping test server download is opt-in",
)
@pytest.mark.asyncio
async def test_temporal_delivery_reaches_healthy_without_production_approval():
    digest = "sha256:" + "c" * 64
    delivery = DeliveryInput(
        application_id="app-1",
        pipeline_run_id="run-1",
        runtime="docker",
        environment="dev",
        artifact_digest=digest,
        parameters={"health_url": "http://example.invalid/healthz"},
    )
    activities = DeliveryActivities(InMemoryEvidenceStore(digest), HealthyRuntimeRunner())

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
            ],
        ):
            result = await environment.client.execute_workflow(
                ProvisionAndDeployWorkflow.run,
                delivery,
                id=f"workflow-{uuid.uuid4()}",
                task_queue=task_queue,
            )

    assert result.status == "healthy"
    assert result.deployment_id == "deployment-1"
    assert result.artifact_digest == digest
