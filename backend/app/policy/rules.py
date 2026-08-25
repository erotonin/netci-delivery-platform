from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from ..domain.models import Environment, Runtime


class PolicyViolation(Exception):
    pass


class Role(str, Enum):
    DEVELOPER = "developer"
    REVIEWER = "reviewer"
    PLATFORM_ADMIN = "platform-admin"


@dataclass(frozen=True)
class ArtifactEvidence:
    digest: str | None
    sbom_present: bool
    vulnerability_scan_passed: bool
    signature_verified: bool


def require_deployable_artifact(evidence: ArtifactEvidence) -> None:
    if not evidence.digest or not evidence.digest.startswith("sha256:"):
        raise PolicyViolation("deployment requires an immutable sha256 artifact digest")
    if not evidence.sbom_present:
        raise PolicyViolation("deployment requires SBOM evidence")
    if not evidence.vulnerability_scan_passed:
        raise PolicyViolation("deployment blocked by vulnerability policy")
    if not evidence.signature_verified:
        raise PolicyViolation("deployment requires verified artifact signature")


def require_environment_permission(environment: Environment, role: Role) -> None:
    if environment == Environment.PROD and role not in {Role.REVIEWER, Role.PLATFORM_ADMIN}:
        raise PolicyViolation("production deployment requires reviewer or platform-admin role")


def require_runtime_supported(runtime: Runtime) -> None:
    if runtime not in {Runtime.DOCKER, Runtime.KUBERNETES, Runtime.SYSTEMD}:
        raise PolicyViolation(f"unsupported runtime: {runtime}")
