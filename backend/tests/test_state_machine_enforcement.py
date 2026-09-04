"""The state machine is enforced, and a repair may not invent what it does not know.

Two regressions this pins, both real:

1. `PIPELINE_TRANSITIONS` was a table nothing consulted. The rules therefore lived twice
   -- once in the table, once as scattered `if` statements -- and drifted: `QUEUED ->
   SUCCEEDED` was added, which lets a run that never started report a successful build.

2. The reconciler closed a run whose digest it never received by substituting
   `sha256:000...0`. That digest passes the immutability regex, so the run was marked
   successful and a deployment was created for an artifact that does not exist.
"""

from __future__ import annotations

import pytest

from app.delivery import DeliveryError, DeliveryPlatform
from app.domain.models import (
    DeploymentStatus,
    Environment,
    PipelineStatus,
    Runtime,
    can_transition_deployment,
    can_transition_pipeline,
)

DIGEST = "sha256:" + "b" * 64


def platform() -> DeliveryPlatform:
    return DeliveryPlatform()


def queued_run(engine, name="sm-app", environment=Environment.STAGING):
    application = engine.create_application(
        name=name,
        repository_url=f"https://github.com/example/{name}",
        pipeline_template="container-ci-cd-v1",
        runtime=Runtime.DOCKER,
        default_environment=Environment.DEV,
        stages=[],
        idempotency_key=None,
    )
    run = engine.start_pipeline(
        application.id,
        commit_sha="abc1234",
        branch="main",
        environment=environment,
        parameters={},
        correlation_id="state-machine",
        idempotency_key=None,
    )
    assert run.status == PipelineStatus.QUEUED
    return application, run


# ------------------------------------------------------------------- the table


def test_a_queued_run_cannot_reach_succeeded():
    """Nothing ran, so there is no success to report."""

    assert not can_transition_pipeline(PipelineStatus.QUEUED, PipelineStatus.SUCCEEDED)


def test_a_queued_run_can_still_be_closed():
    """The states a repair legitimately needs stay reachable."""

    assert can_transition_pipeline(PipelineStatus.QUEUED, PipelineStatus.RUNNING)
    assert can_transition_pipeline(PipelineStatus.QUEUED, PipelineStatus.FAILED)
    assert can_transition_pipeline(PipelineStatus.QUEUED, PipelineStatus.CANCELLED)


# --------------------------------------------------------------- enforcement


def test_the_domain_refuses_a_transition_the_table_forbids():
    """The table is load-bearing, not documentation. This is what stops it drifting."""

    engine = platform()
    _, run = queued_run(engine)

    with pytest.raises(DeliveryError) as failure:
        engine.record_ci_result(run.id, PipelineStatus.SUCCEEDED.value, DIGEST, [])

    assert failure.value.code == "INVALID_PIPELINE_STATE"
    assert failure.value.status_code == 409
    assert "queued" in failure.value.message and "succeeded" in failure.value.message
    # And the run is untouched: no deployment was created for a build that never ran.
    assert engine.get_pipeline(run.id).status == PipelineStatus.QUEUED
    assert engine.list_deployments() == ()


def test_a_queued_run_may_still_be_failed():
    engine = platform()
    _, run = queued_run(engine, "sm-fail")

    engine.record_ci_result(run.id, PipelineStatus.FAILED.value, None, ["never started"])

    assert engine.get_pipeline(run.id).status == PipelineStatus.FAILED


def test_the_normal_path_is_unaffected():
    engine = platform()
    _, run = queued_run(engine, "sm-normal")

    engine.record_ci_result(run.id, PipelineStatus.RUNNING.value, None, [])
    engine.record_security_evidence(run.id, {
        "artifactDigest": DIGEST,
        "sbom": {"generatedBy": "syft", "location": "s3://evidence/sbom.json"},
        "vulnerabilityScan": {"scanner": "trivy", "status": "passed", "critical": 0, "high": 0},
        "signature": {"provider": "cosign", "verified": True},
    })
    result = engine.record_ci_result(run.id, PipelineStatus.SUCCEEDED.value, DIGEST, [])

    assert result.deployment is not None
    assert engine.get_pipeline(run.id).status == PipelineStatus.RUNNING  # healthy deploy completes it


def test_deployment_transitions_are_enforced_from_the_same_table():
    engine = platform()
    _, run = queued_run(engine, "sm-deploy")
    engine.record_ci_result(run.id, PipelineStatus.RUNNING.value, None, [])
    engine.record_security_evidence(run.id, {
        "artifactDigest": DIGEST,
        "sbom": {"generatedBy": "syft", "location": "s3://evidence/sbom.json"},
        "vulnerabilityScan": {"scanner": "trivy", "status": "passed", "critical": 0, "high": 0},
        "signature": {"provider": "cosign", "verified": True},
    })
    deployment = engine.record_ci_result(run.id, PipelineStatus.SUCCEEDED.value, DIGEST, []).deployment
    assert deployment is not None
    engine.record_deployment_result(deployment.id, DeploymentStatus.HEALTHY.value, "ok")

    # healthy -> deploying is not in the table, and must not be reachable.
    assert not can_transition_deployment(DeploymentStatus.HEALTHY, DeploymentStatus.DEPLOYING)
    with pytest.raises(DeliveryError) as failure:
        engine.record_deployment_result(deployment.id, DeploymentStatus.FAILED.value, "late")
    assert failure.value.code == "INVALID_DEPLOYMENT_STATE"


# ------------------------------------------------- the reconciler may not invent


class SucceedingCi:
    """A CI engine that reports success. What netCI knows about it is the variable."""

    mode = "stub"

    def __init__(self, status: str = "succeeded") -> None:
        self.status = status

    def launch(self, request):
        return None

    def get_status(self, external_id):
        return self.status

    def abort(self, external_id):
        return True


def test_a_success_without_a_digest_is_not_recorded_as_success():
    """The bug: `sha256:000...0` was substituted, so a nonexistent artifact looked shippable."""

    from app.reconciler import Reconciler

    engine = platform()
    _, run = queued_run(engine, "sm-nodigest")
    reconciler = Reconciler(engine, SucceedingCi(), None)

    reconciler._reconcile_one_run(engine.get_pipeline(run.id), timeout_seconds=99999)

    repaired = engine.get_pipeline(run.id)
    assert repaired.status == PipelineStatus.FAILED, "a success netCI cannot name is not a success"
    assert repaired.artifact_digest is None
    assert engine.list_deployments() == (), "no deployment for an artifact that does not exist"
    _, lines = engine.get_pipeline_logs(run.id)
    assert any("cannot be identified" in line for line in lines)
    assert not any("0" * 64 in line for line in lines)


def test_repairing_a_lost_callback_records_the_transition_it_missed():
    """`queued -> succeeded` is illegal, so the repair reconstructs `running` first."""

    from app.reconciler import Reconciler

    engine = platform()
    _, run = queued_run(engine, "sm-lost")
    # netCI has the digest -- CI published evidence -- but never got the result callback.
    engine.record_ci_result(run.id, PipelineStatus.RUNNING.value, None, [])
    engine.record_security_evidence(run.id, {
        "artifactDigest": DIGEST,
        "sbom": {"generatedBy": "syft", "location": "s3://evidence/sbom.json"},
        "vulnerabilityScan": {"scanner": "trivy", "status": "passed", "critical": 0, "high": 0},
        "signature": {"provider": "cosign", "verified": True},
    })
    engine.record_ci_result(run.id, PipelineStatus.SUCCEEDED.value, DIGEST, [])
    current = engine.get_pipeline(run.id)
    assert current.artifact_digest == DIGEST

    reconciler = Reconciler(engine, SucceedingCi(), None)
    # A run whose CI result already landed is not a candidate for repair, even though it
    # stays `running` while its deployment executes. Re-reporting it would build a second
    # deployment for the same artifact.
    repaired = reconciler.reconcile_runs()

    assert repaired == []
    assert engine.get_pipeline(run.id).artifact_digest == DIGEST
    assert len(engine.list_deployments()) == 1, "the reconciler must not duplicate a deployment"
