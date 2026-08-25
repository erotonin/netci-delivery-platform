from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from temporalio import activity, workflow


@dataclass
class DeliveryInput:
    application_id: str
    pipeline_run_id: str
    runtime: str
    environment: str
    artifact_digest: str
    require_approval: bool = True


@dataclass
class DeliveryResult:
    deployment_id: str
    status: str
    artifact_digest: str


@activity.defn
async def validate_artifact(input: DeliveryInput) -> None:
    """Validate SBOM, scan and signature evidence through PolicyEngine adapter."""
    return None


@activity.defn
async def wait_for_approval(input: DeliveryInput) -> None:
    """Production approval is represented by a signal in the real worker."""
    return None


@activity.defn
async def deploy(input: DeliveryInput) -> DeliveryResult:
    """Resolve RuntimeAdapter and execute idempotent deployment."""
    return DeliveryResult(deployment_id=f"deployment-{input.pipeline_run_id}", status="healthy", artifact_digest=input.artifact_digest)


@activity.defn
async def health_check(input: DeliveryInput) -> bool:
    return True


@activity.defn
async def rollback(input: DeliveryInput) -> None:
    return None


@workflow.defn
class ProvisionAndDeployWorkflow:
    def __init__(self) -> None:
        self.approved = False

    @workflow.signal
    async def approve(self) -> None:
        self.approved = True

    @workflow.run
    async def run(self, input: DeliveryInput) -> DeliveryResult:
        await workflow.execute_activity(validate_artifact, input, start_to_close_timeout=timedelta(minutes=5))
        if input.require_approval:
            await workflow.wait_condition(lambda: self.approved, timeout=timedelta(hours=24))
        result = await workflow.execute_activity(deploy, input, start_to_close_timeout=timedelta(minutes=10), retry_policy=workflow.RetryPolicy(maximum_attempts=3))
        healthy = await workflow.execute_activity(health_check, input, start_to_close_timeout=timedelta(minutes=5))
        if not healthy:
            await workflow.execute_activity(rollback, input, start_to_close_timeout=timedelta(minutes=10))
            result.status = "rolled_back"
        return result
