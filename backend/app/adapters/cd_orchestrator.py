"""Seam between the delivery domain and the durable CD workflow engine.

Per ADR-003 only the long-running deploy/approve/health/rollback process becomes a
Temporal workflow.  The domain calls `start` once a deployment exists and `signal_approval`
once a reviewer approves; everything else stays plain application state.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from typing import Protocol
from uuid import UUID, uuid4

from ..runtime_environment import require_live_mode

logger = logging.getLogger(__name__)


class CdStartError(RuntimeError):
    """Raised when the durable workflow could not be started or signalled."""


@dataclass(frozen=True)
class CdStartRequest:
    application_id: UUID
    pipeline_run_id: UUID
    deployment_id: UUID
    runtime: str
    environment: str
    artifact_digest: str
    release_name: str
    require_approval: bool
    parameters: dict[str, object]
    commit_sha: str = ""
    source_repository: str = ""

    @property
    def workflow_id(self) -> str:
        """Deterministic id so a retried callback re-attaches instead of duplicating."""

        return f"netci-deploy-{self.deployment_id}"

    @property
    def rollback_workflow_id(self) -> str:
        """One id per rollback *generation*: a deployment may be rolled back, redeployed
        and rolled back again, and the fencing token is what changes between those."""

        generation = self.parameters.get("fencing_token")
        return f"netci-rollback-{self.deployment_id}-{generation if generation is not None else 0}"


@dataclass(frozen=True)
class PreviewStartRequest:
    """Start or stop a pull request's own deployment (ADR-049)."""

    preview_id: str
    application_id: UUID
    pipeline_run_id: UUID
    action: str  # "deploy" | "teardown"
    namespace: str
    release: str
    artifact_digest: str = ""
    parameters: dict[str, object] = field(default_factory=dict)

    @property
    def workflow_id(self) -> str:
        """Unlike a deployment's, not deterministic: a preview is (re)started by writing
        a new row transition each time, never by resuming one a caller lost track of, so
        there is nothing to re-attach to and a collision would only ever be a bug."""

        return f"netci-preview-{self.preview_id}-{self.action}-{uuid4().hex[:8]}"


class CdOrchestrator(Protocol):
    def start(self, request: CdStartRequest) -> str | None: ...

    def start_rollback(self, request: CdStartRequest) -> str | None: ...

    def start_preview(self, request: PreviewStartRequest) -> str | None: ...

    def signal_approval(self, workflow_id: str, actor: str, comment: str) -> None: ...

    def cancel(self, workflow_id: str) -> None: ...

    def get_status(self, workflow_id: str) -> str | None: ...


class NullCdOrchestrator:
    """Default: netCI tracks deployment state and waits for an external result callback."""

    mode = "none"

    def start(self, request: CdStartRequest) -> str | None:
        return None

    def start_rollback(self, request: CdStartRequest) -> str | None:
        return None

    def start_preview(self, request: PreviewStartRequest) -> str | None:
        return None

    def signal_approval(self, workflow_id: str, actor: str, comment: str) -> None:
        return None

    def cancel(self, workflow_id: str) -> None:
        return None

    def get_status(self, workflow_id: str) -> str | None:
        return None


class TemporalCdOrchestrator:
    """Start and signal `ProvisionAndDeployWorkflow` on a real Temporal server."""

    mode = "temporal"

    def __init__(self, address: str, namespace: str, task_queue: str, *, timeout_seconds: float = 10.0) -> None:
        self.address = address
        self.namespace = namespace
        self.task_queue = task_queue
        self.timeout_seconds = timeout_seconds

    def _run(self, coroutine):
        """Bridge to Temporal's async client from the synchronous domain service."""

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(asyncio.wait_for(coroutine, self.timeout_seconds))
        # Called from inside a running loop (async test client): use a private loop
        # in a worker thread so we never re-enter the caller's loop.
        import concurrent.futures

        def runner():
            return asyncio.run(asyncio.wait_for(coroutine, self.timeout_seconds))

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(runner).result(timeout=self.timeout_seconds + 5)

    async def _client(self):
        from temporalio.client import Client

        return await Client.connect(self.address, namespace=self.namespace)

    def start(self, request: CdStartRequest) -> str:
        from temporalio.common import WorkflowIDReusePolicy

        from ..workflows.provision_and_deploy import DeliveryInput, ProvisionAndDeployWorkflow

        delivery = DeliveryInput(
            application_id=str(request.application_id),
            pipeline_run_id=str(request.pipeline_run_id),
            runtime=request.runtime,
            environment=request.environment,
            artifact_digest=request.artifact_digest,
            deployment_id=str(request.deployment_id),
            release_name=request.release_name,
            parameters=dict(request.parameters),
            require_approval=request.require_approval,
            commit_sha=request.commit_sha,
            source_repository=request.source_repository,
        )

        async def start_workflow() -> str:
            client = await self._client()
            handle = await client.start_workflow(
                ProvisionAndDeployWorkflow.run,
                delivery,
                id=request.workflow_id,
                task_queue=self.task_queue,
                id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE_FAILED_ONLY,
            )
            return handle.id

        try:
            return self._run(start_workflow())
        except Exception as exc:
            raise CdStartError(f"cannot start delivery workflow {request.workflow_id}: {exc}") from exc

    def start_rollback(self, request: CdStartRequest) -> str:
        """Run RollbackWorkflow for a rollback an operator just requested."""

        from temporalio.common import WorkflowIDReusePolicy

        from ..workflows.provision_and_deploy import DeliveryInput, RollbackWorkflow

        delivery = DeliveryInput(
            application_id=str(request.application_id),
            pipeline_run_id=str(request.pipeline_run_id),
            runtime=request.runtime,
            environment=request.environment,
            artifact_digest=request.artifact_digest,
            deployment_id=str(request.deployment_id),
            release_name=request.release_name,
            parameters=dict(request.parameters),
            require_approval=False,
            commit_sha=request.commit_sha,
            source_repository=request.source_repository,
        )

        async def start_workflow() -> str:
            client = await self._client()
            handle = await client.start_workflow(
                RollbackWorkflow.run,
                delivery,
                id=request.rollback_workflow_id,
                task_queue=self.task_queue,
                id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE_FAILED_ONLY,
            )
            return handle.id

        try:
            return self._run(start_workflow())
        except Exception as exc:  # noqa: BLE001 - surfaced as CdStartError to the caller
            raise CdStartError(f"cannot start rollback workflow {request.rollback_workflow_id}: {exc}") from exc

    def start_preview(self, request: PreviewStartRequest) -> str:
        from temporalio.common import WorkflowIDReusePolicy

        from ..workflows.provision_and_deploy import PreviewInput, PreviewWorkflow

        preview = PreviewInput(
            application_id=str(request.application_id),
            pipeline_run_id=str(request.pipeline_run_id),
            preview_id=request.preview_id,
            action=request.action,
            namespace=request.namespace,
            release=request.release,
            artifact_digest=request.artifact_digest,
            parameters=dict(request.parameters),
        )

        async def start_workflow() -> str:
            client = await self._client()
            handle = await client.start_workflow(
                PreviewWorkflow.run,
                preview,
                id=request.workflow_id,
                task_queue=self.task_queue,
                id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE_FAILED_ONLY,
            )
            return handle.id

        try:
            return self._run(start_workflow())
        except Exception as exc:
            raise CdStartError(f"cannot start preview workflow {request.workflow_id}: {exc}") from exc

    def signal_approval(self, workflow_id: str, actor: str, comment: str) -> None:
        from ..workflows.provision_and_deploy import Approval, ProvisionAndDeployWorkflow

        async def signal() -> None:
            client = await self._client()
            handle = client.get_workflow_handle(workflow_id)
            await handle.signal(ProvisionAndDeployWorkflow.approve, Approval(actor=actor, comment=comment))

        try:
            self._run(signal())
        except Exception as exc:
            raise CdStartError(f"cannot signal approval to workflow {workflow_id}: {exc}") from exc

    def cancel(self, workflow_id: str) -> None:
        async def cancel_workflow() -> None:
            client = await self._client()
            handle = client.get_workflow_handle(workflow_id)
            await handle.cancel()

        try:
            self._run(cancel_workflow())
        except Exception as exc:
            logger.warning("cannot cancel workflow %s: %s", workflow_id, exc)
            raise CdStartError(f"cannot cancel workflow {workflow_id}: {exc}") from exc

    def get_status(self, workflow_id: str) -> str | None:
        async def describe_workflow() -> str | None:
            client = await self._client()
            handle = client.get_workflow_handle(workflow_id)
            desc = await handle.describe()
            return desc.status.name.lower() if desc and desc.status else None

        try:
            return self._run(describe_workflow())
        except Exception:
            return None


def build_cd_orchestrator() -> CdOrchestrator:
    """Compose the configured orchestrator from the environment."""

    mode = os.getenv("NETCI_CD_MODE", "none").strip().lower()
    require_live_mode("NETCI_CD_MODE", mode)
    if mode in {"", "none", "callback"}:
        return NullCdOrchestrator()
    if mode != "temporal":
        raise ValueError(f"unsupported NETCI_CD_MODE: {mode}")
    return TemporalCdOrchestrator(
        address=os.getenv("TEMPORAL_ADDRESS", "localhost:7233"),
        namespace=os.getenv("TEMPORAL_NAMESPACE", "default"),
        task_queue=os.getenv("TEMPORAL_TASK_QUEUE", "netci-delivery"),
    )
