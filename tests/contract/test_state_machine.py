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


def test_a_rollback_passes_through_an_in_progress_state():
    """`rolled_back` is the outcome, not the act.

    "we are putting the old version back" and "the old version is back" are different
    things to an operator deciding whether to page someone, so a rollback is no longer a
    single instantaneous transition.
    """

    assert can_transition_deployment(
        DeploymentStatus.FAILED, DeploymentStatus.ROLLBACK_IN_PROGRESS
    )
    assert can_transition_deployment(
        DeploymentStatus.HEALTHY, DeploymentStatus.ROLLBACK_IN_PROGRESS
    )
    assert can_transition_deployment(
        DeploymentStatus.ROLLBACK_IN_PROGRESS, DeploymentStatus.ROLLED_BACK
    )


def test_a_rollback_that_did_not_work_has_its_own_terminal_state():
    """Calling it `failed` would lose the fact that recovery was attempted."""

    assert can_transition_deployment(
        DeploymentStatus.ROLLBACK_IN_PROGRESS, DeploymentStatus.ROLLBACK_FAILED
    )
    # And it is not the end: someone will try again.
    assert can_transition_deployment(
        DeploymentStatus.ROLLBACK_FAILED, DeploymentStatus.ROLLBACK_IN_PROGRESS
    )
    assert can_transition_deployment(
        DeploymentStatus.ROLLBACK_FAILED, DeploymentStatus.DEPLOYING
    )


def test_a_rolled_back_or_cancelled_deployment_is_final():
    for status in (DeploymentStatus.ROLLED_BACK, DeploymentStatus.CANCELLED):
        assert all(
            not can_transition_deployment(status, target) for target in DeploymentStatus
        ), status
