"""Background reconciliation watchdog for active pipelines and deployments.

Polls active Jenkins builds and Temporal workflows with bounded concurrency,
detects lost callbacks or timeouts, repairs state atomically, and audits every
repair action (pipeline.reconciled, deployment.reconciled).
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any

from .adapters.cd_orchestrator import CdOrchestrator
from .adapters.ci_launcher import CiLauncher
from .domain.models import DeploymentStatus, PipelineRun, PipelineStatus
from .delivery import DeliveryPlatform
from .persistence import AuditRecord, UnitOfWork

logger = logging.getLogger(__name__)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Reconciler:
    def __init__(
        self,
        platform: DeliveryPlatform,
        ci_launcher: CiLauncher,
        cd_orchestrator: CdOrchestrator,
        *,
        default_timeout_seconds: int = 3600,
        max_concurrency: int = 5,
    ) -> None:
        self.platform = platform
        self.ci_launcher = ci_launcher
        self.cd_orchestrator = cd_orchestrator
        self.default_timeout_seconds = default_timeout_seconds
        self.max_concurrency = max_concurrency

    def reconcile(
        self,
        *,
        limit: int = 50,
        timeout_seconds: int | None = None,
    ) -> dict[str, Any]:
        timeout = timeout_seconds if timeout_seconds is not None else self.default_timeout_seconds
        reconciled_runs = self.reconcile_runs(limit=limit, timeout_seconds=timeout)
        reconciled_deployments = self.reconcile_deployments(limit=limit, timeout_seconds=timeout)
        return {
            "reconciledRuns": reconciled_runs,
            "reconciledDeployments": reconciled_deployments,
        }

    def reconcile_runs(
        self,
        *,
        limit: int = 50,
        timeout_seconds: int = 3600,
    ) -> list[dict[str, Any]]:
        with self.platform._transaction() as transaction:
            all_runs = transaction.pipeline_runs()
        # A run stays `running` while its deployment executes, so "status is running" is
        # not the same as "CI has not reported". A run that already carries an artifact
        # digest has received its CI result; re-reporting it would create a second
        # deployment for the same build -- and, once leases exist, that second deployment
        # is refused with DEPLOYMENT_TARGET_BUSY, turning a healthy release into a
        # reconciler-generated error. The digest is what says CI is done.
        active = [
            r for r in all_runs
            if r.status in {PipelineStatus.QUEUED, PipelineStatus.RUNNING}
            and not r.artifact_digest
        ][:limit]

        if not active:
            return []

        results: list[dict[str, Any]] = []
        workers = min(self.max_concurrency, len(active))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(self._reconcile_one_run, run, timeout_seconds): run for run in active}
            for fut in as_completed(futures):
                try:
                    res = fut.result()
                    if res is not None:
                        results.append(res)
                except Exception as exc:
                    logger.warning("reconciler: error reconciling run: %s", exc)
        return results

    def _repair_lost_running_transition(self, run: PipelineRun) -> None:
        """Move a still-queued run to `running` before applying a terminal result.

        `queued -> succeeded` is not a legal transition, and deliberately so: a queued run
        has not started, so there is no build whose success could be reported. When the
        engine says otherwise, the truth is that the `running` callback was lost, not that
        the run skipped execution. Recording it keeps the repaired history honest and
        keeps the state machine as the single description of what may happen.
        """

        if run.status != PipelineStatus.QUEUED:
            return
        self.platform.record_ci_result(
            run.id,
            result_status="running",
            artifact_digest=None,
            log_lines=["reconciled: the run had started in the CI engine; its start callback was lost"],
        )

    def _reconcile_one_run(self, run: PipelineRun, timeout_seconds: int) -> dict[str, Any] | None:
        now = _utc_now()
        age = (now - run.created_at).total_seconds()

        target_ci_id = run.jenkins_run_id or str(run.id)
        try:
            ext_status = self.ci_launcher.get_status(target_ci_id)
        except Exception as exc:
            logger.debug("reconciler: error checking Jenkins status for %s: %s", target_ci_id, exc)
            ext_status = None

        if ext_status is not None:
            ext_status_str = ext_status.lower() if isinstance(ext_status, str) else str(ext_status).lower()
            if ext_status_str in {"succeeded", "success"}:
                # A success netCI cannot name is not a success it may record.
                #
                # This used to substitute `sha256:000...0` when the run carried no digest.
                # That digest passes the immutability regex, so the run would be marked
                # successful and a deployment created for an artifact that does not exist.
                # Inventing an artifact identity to close a run is the exact false-green
                # this platform exists to prevent, so the run is failed with a reason a
                # human can act on instead.
                if not run.artifact_digest:
                    logger.warning(
                        "reconciler: run %s succeeded in Jenkins but netCI never received "
                        "its artifact digest; failing the run rather than inventing one",
                        run.id,
                    )
                    self.platform.record_ci_result(
                        run.id,
                        result_status="failed",
                        artifact_digest=None,
                        log_lines=[
                            "reconciled: the CI engine reports this build succeeded, but its "
                            "artifact digest never reached netCI. The build output cannot be "
                            "identified, so it cannot be deployed. Re-run the pipeline."
                        ],
                    )
                    self._audit_run_reconciled(run, "jenkins_success_without_digest", ext_status_str)
                    return {
                        "pipelineRunId": str(run.id),
                        "action": "reconciled_success_without_digest",
                        "externalStatus": ext_status_str,
                    }

                logger.info("reconciler: run %s succeeded in Jenkins, repairing", run.id)
                # A queued run never received its `running` callback. Record that lost
                # transition first, so the repaired history says what actually happened
                # rather than skipping a state the run really passed through.
                self._repair_lost_running_transition(run)
                self.platform.record_ci_result(
                    run.id,
                    result_status="succeeded",
                    artifact_digest=run.artifact_digest,
                    log_lines=["reconciled: build completed successfully in Jenkins (callback lost)"],
                )
                self._audit_run_reconciled(run, "jenkins_success_callback_lost", ext_status_str)
                return {"pipelineRunId": str(run.id), "action": "reconciled_succeeded", "externalStatus": ext_status_str}

            if ext_status_str in {"failed", "failure", "unstable"}:
                logger.info("reconciler: run %s failed in Jenkins, repairing", run.id)
                self.platform.record_ci_result(
                    run.id,
                    result_status="failed",
                    artifact_digest=None,
                    log_lines=[f"reconciled: build failed in Jenkins with status {ext_status_str}"],
                )
                self._audit_run_reconciled(run, "jenkins_failure_reconciled", ext_status_str)
                return {"pipelineRunId": str(run.id), "action": "reconciled_failed", "externalStatus": ext_status_str}

            if ext_status_str in {"aborted", "cancelled"}:
                logger.info("reconciler: run %s cancelled in Jenkins, repairing", run.id)
                self.platform.cancel_pipeline(run.id, actor="reconciler", reason=f"Jenkins status was {ext_status_str}")
                self._audit_run_reconciled(run, "jenkins_cancelled_reconciled", ext_status_str)
                return {"pipelineRunId": str(run.id), "action": "reconciled_cancelled", "externalStatus": ext_status_str}

        if age > timeout_seconds:
            logger.warning("reconciler: run %s timed out after %ds without terminal status, marking failed", run.id, age)
            self.platform.record_ci_result(
                run.id,
                result_status="failed",
                artifact_digest=None,
                log_lines=[f"reconciled: run timed out after {int(age)}s without reporting back"],
            )
            self._audit_run_reconciled(run, "timeout_lost_callback", None)
            return {"pipelineRunId": str(run.id), "action": "reconciled_timeout_failed"}

        return None

    def _audit_run_reconciled(self, run: PipelineRun, reason: str, external_status: str | None) -> None:
        with self.platform._transaction() as tx:
            unit = UnitOfWork()
            unit.audit.append(
                AuditRecord(
                    "pipeline.reconciled",
                    application_id=run.application_id,
                    pipeline_run_id=run.id,
                    correlation_id=run.correlation_id,
                    payload={"reason": reason, "externalStatus": external_status},
                )
            )
            self.platform._apply(tx, unit)

    def reconcile_deployments(
        self,
        *,
        limit: int = 50,
        timeout_seconds: int = 3600,
    ) -> list[dict[str, Any]]:
        self.platform.recover_expired_leases()

        with self.platform._transaction() as transaction:
            all_deps = transaction.deployments()
        active = [d for d in all_deps if d.status == DeploymentStatus.DEPLOYING][:limit]

        if not active:
            return []

        results: list[dict[str, Any]] = []
        workers = min(self.max_concurrency, len(active))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(self._reconcile_one_deployment, dep, timeout_seconds): dep for dep in active}
            for fut in as_completed(futures):
                try:
                    res = fut.result()
                    if res is not None:
                        results.append(res)
                except Exception as exc:
                    logger.warning("reconciler: error reconciling deployment: %s", exc)
        return results

    def _reconcile_one_deployment(self, deployment, timeout_seconds: int) -> dict[str, Any] | None:
        now = _utc_now()
        age = (now - deployment.created_at).total_seconds()

        with self.platform._transaction() as tx:
            run = tx.pipeline_run(deployment.pipeline_run_id) if deployment.pipeline_run_id else None

        workflow_id = run.workflow_id if run and run.workflow_id else f"netci-deploy-{deployment.id}"
        try:
            ext_status = self.cd_orchestrator.get_status(workflow_id)
        except Exception as exc:
            logger.debug("reconciler: error checking Temporal status for %s: %s", workflow_id, exc)
            ext_status = None

        if ext_status in {"completed"}:
            logger.info("reconciler: deployment %s completed in Temporal, repairing", deployment.id)
            self.platform.record_deployment_result(
                deployment.id,
                result_status="healthy",
                message="reconciled: Temporal workflow completed (callback lost)",
                fencing_token=deployment.fencing_token,
            )
            self._audit_deployment_reconciled(deployment, "temporal_completed_callback_lost", ext_status)
            return {"deploymentId": str(deployment.id), "action": "reconciled_healthy", "externalStatus": ext_status}

        if ext_status in {"failed", "terminated", "timed_out"}:
            logger.info("reconciler: deployment %s failed in Temporal, repairing", deployment.id)
            self.platform.record_deployment_result(
                deployment.id,
                result_status="failed",
                message=f"reconciled: Temporal workflow ended with status {ext_status}",
                fencing_token=deployment.fencing_token,
            )
            self._audit_deployment_reconciled(deployment, "temporal_failed_reconciled", ext_status)
            return {"deploymentId": str(deployment.id), "action": "reconciled_failed", "externalStatus": ext_status}

        if ext_status in {"canceled", "cancelled"}:
            logger.info("reconciler: deployment %s cancelled in Temporal, repairing", deployment.id)
            self.platform.cancel_deployment(deployment.id, actor="reconciler", reason="Temporal workflow was cancelled")
            self._audit_deployment_reconciled(deployment, "temporal_cancelled_reconciled", ext_status)
            return {"deploymentId": str(deployment.id), "action": "reconciled_cancelled", "externalStatus": ext_status}

        if age > timeout_seconds:
            logger.warning("reconciler: deployment %s timed out after %ds, repairing as failed", deployment.id, age)
            self.platform.record_deployment_result(
                deployment.id,
                result_status="failed",
                message=f"reconciled: deployment timed out after {int(age)}s without callback",
                fencing_token=deployment.fencing_token,
            )
            self._audit_deployment_reconciled(deployment, "timeout_lost_callback", None)
            return {"deploymentId": str(deployment.id), "action": "reconciled_timeout_failed"}

        return None

    def _audit_deployment_reconciled(self, deployment, reason: str, external_status: str | None) -> None:
        with self.platform._transaction() as tx:
            unit = UnitOfWork()
            unit.audit.append(
                AuditRecord(
                    "deployment.reconciled",
                    application_id=deployment.application_id,
                    deployment_id=deployment.id,
                    payload={"reason": reason, "externalStatus": external_status},
                )
            )
            self.platform._apply(tx, unit)
