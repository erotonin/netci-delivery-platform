import pytest

from app.policy.rules import ArtifactEvidence, PolicyViolation, Role, require_deployable_artifact, require_environment_permission
from app.domain.models import Environment


def valid_evidence() -> ArtifactEvidence:
    return ArtifactEvidence(
        digest="sha256:abc123",
        sbom_present=True,
        vulnerability_scan_passed=True,
        signature_verified=True,
    )


def test_valid_artifact_is_deployable():
    require_deployable_artifact(valid_evidence())


@pytest.mark.parametrize("field", ["digest", "sbom_present", "vulnerability_scan_passed", "signature_verified"])
def test_invalid_artifact_is_blocked(field):
    values = valid_evidence().__dict__
    values[field] = None if field == "digest" else False
    with pytest.raises(PolicyViolation):
        require_deployable_artifact(ArtifactEvidence(**values))


def test_developer_cannot_deploy_production():
    with pytest.raises(PolicyViolation):
        require_environment_permission(Environment.PROD, Role.DEVELOPER)


def test_reviewer_can_approve_production():
    require_environment_permission(Environment.PROD, Role.REVIEWER)
