# backend/app/coordinator.py
"""Multi-Module Release Plan SAGA Coordinator and Progressive Delivery Engine."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from .domain.models import DeploymentStatus, Environment
from .store.session import PlatformSession
from .traffic import CanaryAnalyzer, default_traffic_router

logger = logging.getLogger(__name__)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class ReleasePlanCoordinator:
    """Coordinates execution of multi-module release waves with SAGA compensation."""

    def __init__(self, portal_service: Any, delivery_platform: Any, traffic_router: Any = None) -> None:
        self.portal = portal_service
        self.platform = delivery_platform
        self.traffic_router = traffic_router or default_traffic_router

    def start_release(self, request_id: str, actor: str) -> dict[str, Any]:
        """Initiate Wave 1 execution for an approved production request."""
        with self.portal._session() as transaction:
            request = transaction.portal_request(request_id)
            if request is None:
                raise KeyError(f"production request {request_id} not found")

            plan = dict(request.release_plan or {})
            waves = plan.get("waves", [])
            if not waves:
                raise ValueError("No release waves found in production request release plan")

            # Mark wave 1 in progress
            waves[0]["status"] = "in_progress"
            plan["waves"] = waves
            transaction.update_portal_request(request_id, status="approved", comment=f"executing wave 1 by {actor}", release_plan=plan)

            wave_1_modules = waves[0].get("moduleIds", [])

        # Dispatch Wave 1 modules
        dispatched_deployments = []
        for mid in wave_1_modules:
            dep = self._dispatch_module_deployment(request_id, mid, actor)
            dispatched_deployments.append(dep)

        return {
            "requestId": request_id,
            "wave": 1,
            "dispatchedCount": len(dispatched_deployments),
            "dispatchedDeployments": [str(d.id) for d in dispatched_deployments],
        }

    def _dispatch_module_deployment(self, request_id: str, module_id: str, actor: str) -> Any:
        with self.portal._session() as transaction:
            request = transaction.portal_request(request_id)
            assert request is not None
            req_mod = next((m for m in request.modules if m.module_id == module_id), None)
            if req_mod is None:
                raise KeyError(f"Module {module_id} not part of request {request_id}")

            version_row = transaction.portal_version(module_id, req_mod.version)
            metadata = version_row.metadata if version_row is not None else {}
            pipeline_run_id = metadata.get("pipelineRunId")
            artifact_digest = metadata.get("artifactDigest")
            if not pipeline_run_id or not artifact_digest:
                raise ValueError(f"Module {module_id} version {req_mod.version} lacks verified pipelineRunId")

            report = metadata.get("ciReport")
            if request.run_automation_tests and (
                not isinstance(report, dict) or report.get("autoTest") != "passed"
            ):
                raise ValueError(f"Module {module_id} requires passing automation-test evidence")

            deployment_parameters = self.portal._delivery_parameters(
                transaction, module_id, Environment.PROD
            )

            # Determine initial traffic parameters based on strategy
            strategy = request.strategy or "rolling"
            traffic_weight = 100
            active_color = None
            if strategy == "canary":
                steps = request.strategy_config.get("steps", [10, 25, 50, 100])
                traffic_weight = steps[0] if steps else 10
            elif strategy == "blue_green":
                active_color = "green"

        deployment = self.platform.create_production_promotion(
            UUID(str(pipeline_run_id)),
            requested_by=request.requested_by,
            correlation_id=f"production-request:{request.id}:{module_id}",
            production_request_id=f"{request.id}:{module_id}",
            scheduled_for=request.scheduled_for,
            rollback_strategy=request.rollback_strategy,
            run_automation_tests=request.run_automation_tests,
            deployment_parameters=deployment_parameters,
        )

        now = _utc_now()
        with self.portal._session() as transaction:
            # Set deployment strategy and initial traffic weight
            transaction.update_deployment_traffic(
                deployment.id,
                strategy=strategy,
                traffic_weight=traffic_weight,
                canary_step=1 if strategy == "canary" else 0,
                active_color=active_color,
            )
            # Link deployment to production request module
            transaction.update_portal_request_module(
                request_id,
                module_id,
                status="deploying",
                deployment_id=deployment.id,
                started_at=now,
            )
            # Also keep primary deployment_id on production_requests updated
            transaction.update_portal_request(
                request_id,
                status="approved",
                comment=f"deploying module {module_id}",
                deployment_id=deployment.id,
            )

        # Route initial traffic if progressive
        if strategy == "canary":
            self.traffic_router.set_traffic_weight(str(deployment.application_id), "prod", traffic_weight)
        elif strategy == "blue_green":
            self.traffic_router.switch_route(str(deployment.application_id), "prod", "green")

        try:
            self.platform.approve_deployment(deployment.id, actor)
        except Exception as exc:
            with self.portal._session() as transaction:
                transaction.update_portal_request_module(
                    request_id,
                    module_id,
                    status="failed",
                    deployment_id=deployment.id,
                    error_message=str(exc),
                    completed_at=_utc_now(),
                )
            raise

        return deployment

    def record_module_deployment_result(
        self,
        request_id: str,
        deployment_id: UUID,
        status: str,
        message: str | None = None,
    ) -> None:
        """Handle deployment result callback, advancing to next wave or triggering reverse compensation."""
        with self.portal._session() as transaction:
            request = transaction.portal_request(request_id)
            if request is None:
                return

            module_row = next((m for m in request.modules if m.deployment_id == deployment_id), None)
            if module_row is None:
                # Fallback matching by primary deployment_id
                if request.deployment_id == deployment_id and request.modules:
                    module_row = request.modules[0]
                else:
                    return

            now = _utc_now()
            is_success = status in ("healthy", "succeeded")
            mod_status = "succeeded" if is_success else "failed"

            transaction.update_portal_request_module(
                request_id,
                module_row.module_id,
                status=mod_status,
                deployment_id=deployment_id,
                error_message=None if is_success else (message or "Deployment failed"),
                completed_at=now,
            )

            # Refresh request to get updated module statuses
            request = transaction.portal_request(request_id)
            assert request is not None
            plan = dict(request.release_plan or {})
            waves = plan.get("waves", [])

        if not is_success:
            self._handle_wave_failure(request_id, module_row.module_id, message or "Deployment failed")
            return

        # Check wave completion
        self._check_and_advance_waves(request_id)

    def _check_and_advance_waves(self, request_id: str) -> None:
        with self.portal._session() as transaction:
            request = transaction.portal_request(request_id)
            if request is None:
                return
            plan = dict(request.release_plan or {})
            waves = plan.get("waves", [])
            modules_by_id = {m.module_id: m for m in request.modules}

            current_wave_idx = None
            for idx, wave in enumerate(waves):
                if wave.get("status") == "in_progress":
                    current_wave_idx = idx
                    break

            if current_wave_idx is None:
                # Check if all waves are succeeded
                all_done = all(
                    all(modules_by_id[mid].status == "succeeded" for mid in wave.get("moduleIds", []) if mid in modules_by_id)
                    for wave in waves
                )
                if all_done and request.status != "succeeded":
                    transaction.update_portal_request(request_id, status="succeeded", comment="all release waves completed successfully")
                return

            current_wave = waves[current_wave_idx]
            current_wave_modules = [modules_by_id[mid] for mid in current_wave.get("moduleIds", []) if mid in modules_by_id]
            wave_all_succeeded = all(m.status == "succeeded" for m in current_wave_modules)

            if not wave_all_succeeded:
                # Still waiting for other modules in this wave
                return

            # Current wave is done!
            current_wave["status"] = "succeeded"
            next_wave_idx = current_wave_idx + 1

            if next_wave_idx < len(waves):
                waves[next_wave_idx]["status"] = "in_progress"
                plan["waves"] = waves
                transaction.update_portal_request(
                    request_id,
                    status="approved",
                    comment=f"wave {current_wave.get('wave')} succeeded, starting wave {waves[next_wave_idx].get('wave')}",
                    release_plan=plan,
                )
                next_modules = waves[next_wave_idx].get("moduleIds", [])
            else:
                plan["waves"] = waves
                transaction.update_portal_request(
                    request_id,
                    status="succeeded",
                    comment="all release waves completed successfully",
                    release_plan=plan,
                )
                next_modules = []

        # Dispatch next wave outside transaction
        for mid in next_modules:
            self._dispatch_module_deployment(request_id, mid, actor="coordinator")

    def _handle_wave_failure(self, request_id: str, failed_module_id: str, reason: str) -> None:
        """SAGA compensation: rollback already succeeded modules in reverse order."""
        logger.error("Release plan failed on module %s in request %s: %s. Initiating reverse rollback.", failed_module_id, request_id, reason)

        with self.portal._session() as transaction:
            request = transaction.portal_request(request_id)
            if request is None:
                return
            plan = dict(request.release_plan or {})
            for wave in plan.get("waves", []):
                if wave.get("status") == "in_progress":
                    wave["status"] = "failed"

            transaction.update_portal_request(
                request_id,
                status="blocked",
                comment=f"failed on module {failed_module_id}: {reason}. SAGA rollback in progress.",
                release_plan=plan,
            )

            # Collect completed deployments in reverse chronological order
            modules_to_rollback = [
                m for m in request.modules
                if m.status == "succeeded" and m.deployment_id is not None
            ]
            modules_to_rollback.reverse()

        # Execute rollback compensation
        rollback_errors = []
        for mod in modules_to_rollback:
            try:
                target_digest = None
                with self.portal._session() as tx:
                    dep = tx.deployment(mod.deployment_id)
                    if dep and dep.previous_artifact_digest:
                        target_digest = dep.previous_artifact_digest
                    elif dep:
                        past = tx.deployments(application_id=dep.application_id)
                        for d in past:
                            if d.id != dep.id and d.status == DeploymentStatus.HEALTHY and d.artifact_digest:
                                target_digest = d.artifact_digest
                                break
                if not target_digest:
                    target_digest = "sha256:" + "0" * 64

                self.platform.rollback_deployment(
                    mod.deployment_id,
                    target_artifact_digest=target_digest,
                )
                with self.portal._session() as transaction:
                    transaction.update_portal_request_module(
                        request_id,
                        mod.module_id,
                        status="rolled_back",
                    )
            except Exception as rollback_err:
                logger.error("SAGA compensation failed for module %s (deployment %s): %s", mod.module_id, mod.deployment_id, rollback_err)
                rollback_errors.append(f"{mod.module_id}: {rollback_err}")

        with self.portal._session() as transaction:
            if rollback_errors:
                final_comment = f"request failed and SAGA rollback encountered errors: {'; '.join(rollback_errors)}"
            else:
                final_comment = f"request failed on {failed_module_id}; SAGA reverse rollback completed successfully."
            transaction.update_portal_request(request_id, status="rejected" if not rollback_errors else "blocked", comment=final_comment)

    def advance_canary(
        self,
        request_id: str,
        deployment_id: UUID,
        metrics: dict[str, float] | None = None,
    ) -> dict[str, Any]:
        """Evaluate canary metrics and advance to the next traffic step."""
        with self.portal._session() as transaction:
            request = transaction.portal_request(request_id)
            if request is None:
                raise KeyError(f"production request {request_id} not found")
            deployment = transaction.deployment(deployment_id)
            if deployment is None:
                raise KeyError(f"deployment {deployment_id} not found")

            config = request.strategy_config or {}
            steps = config.get("steps", [10, 25, 50, 100])
            thresholds = config.get("thresholds", {})

        # Evaluate metrics
        decision = CanaryAnalyzer.evaluate(metrics, thresholds)
        if not decision.allowed:
            # Metrics threshold breached! Auto-abort canary
            self.abort_canary(request_id, deployment_id, reason=decision.reason)
            return {
                "allowed": False,
                "status": "aborted",
                "reason": decision.reason,
                "metrics": {
                    "errorRate": decision.error_rate,
                    "p95LatencyMs": decision.p95_latency_ms,
                },
            }

        current_weight = deployment.traffic_weight
        next_weight = None
        for step in steps:
            if step > current_weight:
                next_weight = step
                break

        if next_weight is None:
            # Already at 100%
            return {
                "allowed": True,
                "status": "completed",
                "trafficWeight": 100,
                "reason": "Canary already at maximum traffic weight (100%)",
            }

        # Apply new traffic weight
        self.traffic_router.set_traffic_weight(str(deployment.application_id), "prod", next_weight)
        next_step_num = deployment.canary_step + 1

        with self.portal._session() as transaction:
            transaction.update_deployment_traffic(
                deployment_id,
                traffic_weight=next_weight,
                canary_step=next_step_num,
            )

        return {
            "allowed": True,
            "status": "advanced",
            "trafficWeight": next_weight,
            "canaryStep": next_step_num,
            "reason": decision.reason,
        }

    def abort_canary(self, request_id: str, deployment_id: UUID, reason: str) -> dict[str, Any]:
        """Abort canary delivery and immediately revert traffic weight to 0%."""
        with self.portal._session() as transaction:
            deployment = transaction.deployment(deployment_id)
            if deployment is None:
                raise KeyError(f"deployment {deployment_id} not found")
            app_id = str(deployment.application_id)

        self.traffic_router.set_traffic_weight(app_id, "prod", 0, baseline_weight=100)

        with self.portal._session() as transaction:
            transaction.update_deployment_traffic(deployment_id, traffic_weight=0)
            transaction.update_portal_request(
                request_id,
                status="blocked",
                comment=f"Canary aborted: {reason}",
            )

        return {
            "status": "aborted",
            "trafficWeight": 0,
            "reason": reason,
        }
