from __future__ import annotations

from typing import Any
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy


def _failure_message(exc: BaseException) -> str:
    """Name the activity and its reason, not the wrapper.

    Temporal surfaces an activity failure as ActivityError -> ApplicationError; reporting
    `ActivityError` alone leaves an operator with a failed deployment and no idea whether
    cosign, DCIM or the playbook refused it. Activity messages are already bounded and
    never carry a credential, so the cause can be quoted as-is; the cap is for the record.
    """

    activity = getattr(exc, "activity_type", None)
    cause = getattr(exc, "cause", None)
    reason = cause if cause is not None else exc
    text = str(getattr(reason, "message", None) or reason or "").strip()
    # ApplicationError carries the worker-side exception class in `.type`.
    kind = str(getattr(reason, "type", None) or type(reason).__name__)
    head = f"activity {activity} failed" if activity else "delivery workflow failed"
    detail = f"{kind}: {text}" if text else kind
    return f"{head}: {detail}"[:1900]


def _int_or_none(value: object) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class DeliveryInput:
    application_id: str
    pipeline_run_id: str
    runtime: str
    environment: str
    artifact_digest: str
    deployment_id: str = ""
    release_name: str = "netci-release"
    parameters: dict[str, Any] = field(default_factory=dict)
    require_approval: bool | None = None
    # The commit the artifact was built from; the runtime health gates compare it.
    commit_sha: str = ""

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
    message: str = ""
    # The lease generation this workflow was started under. netCI refuses a result that
    # carries an older one, which is how a workflow that paused past its lease cannot
    # overwrite the result of the workflow that replaced it.
    fencing_token: int | None = None
    # A token minted for exactly this deployment when the workflow was started. It is
    # what lets the worker report without holding a shared key that could report for
    # any deployment. Never logged; Temporal history is the only place it lives.
    callback_token: str = ""


@dataclass(frozen=True)
class RollbackResult:
    deployment_id: str
    succeeded: bool
    artifact_digest: str
    message: str = ""
    fencing_token: int | None = None
    callback_token: str = ""


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
        try:
            await workflow.execute_activity(
                "validate_artifact",
                delivery,
                start_to_close_timeout=timedelta(minutes=5),
                retry_policy=RetryPolicy(maximum_attempts=1),
            )
            if delivery.approval_required:
                await workflow.wait_condition(lambda: self.approval is not None, timeout=timedelta(hours=24))
            not_before = delivery.parameters.get("notBefore")
            if isinstance(not_before, str):
                scheduled = datetime.fromisoformat(not_before.replace("Z", "+00:00"))
                delay = scheduled - workflow.now()
                if delay.total_seconds() > 0:
                    await workflow.sleep(delay)
            result = await workflow.execute_activity(
                "deploy",
                delivery,
                result_type=DeliveryResult,
                start_to_close_timeout=timedelta(minutes=10),
                heartbeat_timeout=timedelta(seconds=45),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
            result = replace(
                result,
                fencing_token=_int_or_none(delivery.parameters.get("fencing_token")),
                callback_token=str(delivery.parameters.get("callback_token") or ""),
            )
            healthy = await workflow.execute_activity(
                "health_check",
                delivery,
                result_type=bool,
                start_to_close_timeout=timedelta(minutes=5),
                heartbeat_timeout=timedelta(seconds=45),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
            if not healthy:
                if delivery.parameters.get("rollbackStrategy", "automatic") == "automatic":
                    await workflow.execute_activity(
                        "rollback",
                        delivery,
                        start_to_close_timeout=timedelta(minutes=10),
                        heartbeat_timeout=timedelta(seconds=45),
                        retry_policy=RetryPolicy(maximum_attempts=3),
                    )
                    result = replace(
                        result,
                        status="rolled_back",
                        message="health check failed; automatic rollback completed",
                    )
                else:
                    result = replace(
                        result,
                        status="failed",
                        message="health check failed; manual intervention required",
                    )
            else:
                result = replace(result, status="healthy", message="health check passed")
        except Exception as exc:
            failed = DeliveryResult(
                deployment_id=delivery.deployment_id,
                status="failed",
                artifact_digest=delivery.artifact_digest,
                message=_failure_message(exc),
                fencing_token=_int_or_none(delivery.parameters.get("fencing_token")),
                callback_token=str(delivery.parameters.get("callback_token") or ""),
            )
            await workflow.execute_activity(
                "report_deployment_result",
                failed,
                start_to_close_timeout=timedelta(minutes=1),
                retry_policy=RetryPolicy(maximum_attempts=10),
            )
            raise
        await workflow.execute_activity(
            "report_deployment_result",
            result,
            start_to_close_timeout=timedelta(minutes=1),
            retry_policy=RetryPolicy(maximum_attempts=10),
        )
        return result


@workflow.defn
class RollbackWorkflow:
    """Execute an operator-requested rollback, and report whether it restored service.

    `POST /deployments/{id}/rollback` used to write `rollback_in_progress` and stop: no
    worker ran the playbook, and nothing ever reported `/rollback-result`, so a manual
    rollback on a real stack sat in progress forever. The automatic rollback inside
    ProvisionAndDeployWorkflow already ran on the worker; this makes the manual one do
    the same, through the same activities and the same fenced, single-use callback.
    The target artifact is re-verified first: a rollback is a deployment of an older
    digest and gets no exemption from the signature gate.
    """

    @workflow.run
    async def run(self, delivery: DeliveryInput) -> RollbackResult:
        base = RollbackResult(
            deployment_id=delivery.deployment_id,
            succeeded=False,
            artifact_digest=delivery.artifact_digest,
            fencing_token=_int_or_none(delivery.parameters.get("fencing_token")),
            callback_token=str(delivery.parameters.get("callback_token") or ""),
        )
        try:
            await workflow.execute_activity(
                "validate_artifact",
                delivery,
                start_to_close_timeout=timedelta(minutes=5),
                retry_policy=RetryPolicy(maximum_attempts=1),
            )
            await workflow.execute_activity(
                "rollback",
                delivery,
                start_to_close_timeout=timedelta(minutes=10),
                heartbeat_timeout=timedelta(seconds=45),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
            healthy = await workflow.execute_activity(
                "health_check",
                delivery,
                result_type=bool,
                start_to_close_timeout=timedelta(minutes=5),
                heartbeat_timeout=timedelta(seconds=45),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
            result = replace(
                base,
                succeeded=bool(healthy),
                message="rollback restored service" if healthy else "rollback ran but the health check did not pass",
            )
        except Exception as exc:
            result = replace(base, succeeded=False, message=_failure_message(exc))
            await workflow.execute_activity(
                "report_rollback_result",
                result,
                start_to_close_timeout=timedelta(minutes=1),
                retry_policy=RetryPolicy(maximum_attempts=10),
            )
            raise
        await workflow.execute_activity(
            "report_rollback_result",
            result,
            start_to_close_timeout=timedelta(minutes=1),
            retry_policy=RetryPolicy(maximum_attempts=10),
        )
        return result
