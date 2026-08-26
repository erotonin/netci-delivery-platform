from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy


@dataclass(frozen=True)
class DeliveryInput:
    application_id: str
    pipeline_run_id: str
    runtime: str
    environment: str
    artifact_digest: str
    release_name: str = "netci-release"
    parameters: dict[str, object] = field(default_factory=dict)
    require_approval: bool | None = None

    @property
    def approval_required(self) -> bool:
        if self.require_approval is not None:
            return self.require_approval
        return self.environment == "prod"


@dataclass(frozen=True)
class DeliveryResult:
    deployment_id: str
    status: str
    artifact_digest: str


@dataclass(frozen=True)
class Approval:
    actor: str
    comment: str = ""


@workflow.defn
class ProvisionAndDeployWorkflow:
    def __init__(self) -> None:
        self.approval: Approval | None = None

    @workflow.signal
    async def approve(self, approval: Approval) -> None:
        if not approval.actor.strip():
            raise ValueError("approval actor is required")
        self.approval = approval

    @workflow.query
    def approval_status(self) -> Approval | None:
        return self.approval

    @workflow.run
    async def run(self, delivery: DeliveryInput) -> DeliveryResult:
        await workflow.execute_activity(
            "validate_artifact",
            delivery,
            start_to_close_timeout=timedelta(minutes=5),
            retry_policy=RetryPolicy(maximum_attempts=1),
        )
        if delivery.approval_required:
            await workflow.wait_condition(lambda: self.approval is not None, timeout=timedelta(hours=24))
        result = await workflow.execute_activity(
            "deploy",
            delivery,
            result_type=DeliveryResult,
            start_to_close_timeout=timedelta(minutes=10),
            retry_policy=RetryPolicy(maximum_attempts=3),
        )
        healthy = await workflow.execute_activity(
            "health_check",
            delivery,
            result_type=bool,
            start_to_close_timeout=timedelta(minutes=5),
            retry_policy=RetryPolicy(maximum_attempts=3),
        )
        if not healthy:
            await workflow.execute_activity(
                "rollback",
                delivery,
                start_to_close_timeout=timedelta(minutes=10),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
            result = replace(result, status="rolled_back")
        else:
            result = replace(result, status="healthy")
        return result
