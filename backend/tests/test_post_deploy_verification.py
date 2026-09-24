"""Post-deploy verification in the real workflow, on Temporal's test server (ADR-046).

A release the runtime calls healthy is watched through its own metrics for the module's
window; a breach, or a metric that never returns data, takes the rollback path the
health check already uses. The metrics source here is a fake that answers fixed values.
"""

from __future__ import annotations

import uuid

import pytest
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

import app.workflows.activities as activities_module
from app.adapters.prometheus_metrics import MetricsUnavailable
from app.workflows.activities import DeliveryActivities
from app.workflows.provision_and_deploy import DeliveryInput, ProvisionAndDeployWorkflow
from test_temporal_workflow import InMemoryEvidenceStore, RecordingDeploymentReporter, needs_temporal_test_server

DIGEST = "sha256:" + "d" * 64
VERIFICATION = {
    "queries": {"errorRate": 'sum(rate(errors{app="{release}",env="{environment}"}[1m]))'},
    "maxErrorRate": 0.02, "maxP95LatencyMs": 1000, "windowMinutes": 1, "intervalSeconds": 30,
    "environments": ["dev"],
}


class FixedMetrics:
    name = "fake"

    def __init__(self, value):
        self.value = value
        self.queries: list[str] = []

    def query(self, promql):
        self.queries.append(promql)
        if isinstance(self.value, Exception):
            raise self.value
        return self.value


class Runner:
    def __init__(self):
        self.rolled_back = 0

    async def deploy(self, delivery):
        return "deployment-1"

    async def health_check(self, delivery):
        return True

    async def rollback(self, delivery):
        self.rolled_back += 1


@pytest.fixture(autouse=True)
def no_real_waiting(monkeypatch):
    async def instant(_seconds):
        return None
    monkeypatch.setattr(activities_module.asyncio, "sleep", instant)


def _delivery(**parameters):
    return DeliveryInput(application_id="app-1", pipeline_run_id="run-1", runtime="docker", environment="dev",
                         artifact_digest=DIGEST, deployment_id="dep-1",
                         parameters={"app_name": "orders-api", **parameters})


async def _run(delivery, metrics):
    runner, reporter = Runner(), RecordingDeploymentReporter()
    acts = DeliveryActivities(InMemoryEvidenceStore(DIGEST), runner, deployment_reporter=reporter, metrics_source=metrics)
    async with await WorkflowEnvironment.start_time_skipping() as environment:
        queue = f"netci-verify-{uuid.uuid4()}"
        async with Worker(environment.client, task_queue=queue, workflows=[ProvisionAndDeployWorkflow],
                          activities=[acts.validate_artifact, acts.deploy, acts.health_check, acts.verify_release,
                                      acts.rollback, acts.report_deployment_result]):
            result = await environment.client.execute_workflow(
                ProvisionAndDeployWorkflow.run, delivery, id=f"wf-{uuid.uuid4()}", task_queue=queue)
    return result, runner


@needs_temporal_test_server
@pytest.mark.asyncio
async def test_a_release_within_thresholds_for_the_window_is_healthy_and_says_it_was_verified():
    metrics = FixedMetrics(0.001)
    result, runner = await _run(_delivery(verification=VERIFICATION), metrics)
    assert result.status == "healthy" and "verified" in result.message
    assert runner.rolled_back == 0
    assert len(metrics.queries) == 3  # now, +30 s, +60 s
    assert 'app="orders-api"' in metrics.queries[0] and 'env="dev"' in metrics.queries[0]


@needs_temporal_test_server
@pytest.mark.asyncio
async def test_a_breach_rolls_back_at_the_first_sample():
    metrics = FixedMetrics(0.5)
    result, runner = await _run(_delivery(verification=VERIFICATION), metrics)
    assert result.status == "rolled_back"
    assert "post-deploy verification failed" in result.message and "50.00%" in result.message
    assert runner.rolled_back == 1 and len(metrics.queries) == 1


@needs_temporal_test_server
@pytest.mark.asyncio
@pytest.mark.parametrize("value", [None, MetricsUnavailable("prometheus unreachable")])
async def test_no_data_is_a_failure_not_a_pass(value):
    result, runner = await _run(_delivery(verification=VERIFICATION), FixedMetrics(value))
    assert result.status == "rolled_back" and "no data" in result.message
    assert runner.rolled_back == 1


@needs_temporal_test_server
@pytest.mark.asyncio
async def test_manual_rollback_strategy_leaves_a_failed_release_for_a_person():
    result, runner = await _run(_delivery(verification=VERIFICATION, rollbackStrategy="manual"), FixedMetrics(0.5))
    assert result.status == "failed" and "manual intervention" in result.message
    assert runner.rolled_back == 0


@needs_temporal_test_server
@pytest.mark.asyncio
async def test_a_module_without_verification_is_not_queried_and_not_called_verified():
    metrics = FixedMetrics(0.5)
    result, _ = await _run(_delivery(), metrics)
    assert result.status == "healthy" and "verified" not in result.message
    assert metrics.queries == []


@pytest.mark.asyncio
async def test_a_release_name_that_could_change_the_query_is_refused():
    acts = DeliveryActivities(InMemoryEvidenceStore(DIGEST), Runner(), metrics_source=FixedMetrics(0.0))
    verdict = await acts.verify_release(DeliveryInput(
        application_id="a", pipeline_run_id="r", runtime="docker", environment="dev", artifact_digest=DIGEST,
        parameters={"app_name": 'x"} or vector(1) #', "verification": VERIFICATION}))
    assert verdict["passed"] is False and "cannot build the verification queries" in verdict["reason"]
