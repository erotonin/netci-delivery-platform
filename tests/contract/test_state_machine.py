from backend.app.domain.models import (
    DeploymentStatus,
    PipelineStatus,
    can_transition_deployment,
    can_transition_pipeline,
)


def test_pipeline_happy_path_allows_ci_to_approval_to_success():
    assert can_transition_pipeline(PipelineStatus.QUEUED, PipelineStatus.RUNNING)
    assert can_transition_pipeline(PipelineStatus.RUNNING, PipelineStatus.WAITING_APPROVAL)
    assert can_transition_pipeline(PipelineStatus.WAITING_APPROVAL, PipelineStatus.SUCCEEDED)


def test_pipeline_does_not_skip_from_queued_to_success():
    assert not can_transition_pipeline(PipelineStatus.QUEUED, PipelineStatus.SUCCEEDED)


def test_deployment_requires_approval_before_deploying():
    assert can_transition_deployment(DeploymentStatus.PENDING_APPROVAL, DeploymentStatus.DEPLOYING)
    assert not can_transition_deployment(DeploymentStatus.PENDING_APPROVAL, DeploymentStatus.HEALTHY)


def test_failed_deployment_can_rollback():
    assert can_transition_deployment(DeploymentStatus.FAILED, DeploymentStatus.ROLLED_BACK)
