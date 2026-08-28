from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum

from ..domain.models import Environment, Runtime


class PolicyViolation(Exception):
    pass


class Role(str, Enum):
    """What a caller is allowed to do, independent of how they authenticated.

    Ordered by privilege, but deliberately not hierarchical in code: a reviewer is not
    automatically a developer. Granting both is a decision someone makes explicitly in
    the token file or the identity provider, not something inherited by accident.
    """

    VIEWER = "viewer"
    DEVELOPER = "developer"
    REVIEWER = "reviewer"
    PLATFORM_ADMIN = "platform-admin"
    # Machine callers: Jenkins reporting a build result, a worker reporting a deployment.
    # Never granted to a person, and never sufficient to approve anything.
    PIPELINE = "pipeline"


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


def require_environment_permission(environment: Environment, roles: frozenset[Role] | set[Role]) -> None:
    """Production is the environment that needs a second person, so it needs a role.

    Called from the composition root with the roles of the *authenticated* caller. It was
    previously written but never invoked, which made it documentation rather than a
    control; `backend/tests/test_authorization.py` now fails if that happens again.
    """

    if environment == Environment.PROD and not {Role.REVIEWER, Role.PLATFORM_ADMIN}.intersection(roles):
        raise PolicyViolation("production deployment requires reviewer or platform-admin role")


def require_separation_of_duties(requested_by: str, approving: str) -> None:
    """The person who asked for a production release may not be the one who approves it.

    This is the whole point of an approval step. Without it the control degrades into a
    second button press by the same person, which an audit will read as no control at all.
    Set NETCI_REQUIRE_SEPARATION_OF_DUTIES=false only for a single-operator lab.
    """

    if requested_by and approving and requested_by == approving:
        raise PolicyViolation(
            "the requester cannot approve their own production request; "
            "a second person holding the reviewer or platform-admin role must approve it"
        )


def require_runtime_supported(runtime: Runtime) -> None:
    if runtime not in {Runtime.DOCKER, Runtime.KUBERNETES, Runtime.SYSTEMD}:
        raise PolicyViolation(f"unsupported runtime: {runtime}")


@dataclass(frozen=True)
class PolicyDecision:
    """A machine-readable allow/deny with the individual checks behind it."""

    allowed: bool
    reason: str
    checks: dict[str, str] = field(default_factory=dict)

    def as_json(self) -> dict[str, object]:
        return {"decision": "allow" if self.allowed else "deny", "reason": self.reason, "checks": dict(self.checks)}


IMMUTABLE_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


def evaluate_artifact_evidence(
    evidence: dict[str, object] | None,
    *,
    expected_digest: str | None,
    require_evidence: bool,
) -> PolicyDecision:
    """Decide whether an artifact may be deployed, from its CI evidence alone.

    Fail-closed in both directions that matter: evidence that is present must pass
    every check, and missing evidence is refused wherever evidence is required.
    Nothing here trusts a status string the pipeline merely asserted -- each field
    names the tool that produced it.
    """

    checks: dict[str, str] = {}

    if not expected_digest or not IMMUTABLE_DIGEST.fullmatch(expected_digest):
        return PolicyDecision(False, "artifact digest is missing or not an immutable sha256 digest", checks)
    checks["digest"] = "pass"

    if evidence is None:
        if require_evidence:
            return PolicyDecision(False, "no security evidence was published for this artifact", checks)
        return PolicyDecision(True, "security evidence is not required in this environment", {**checks, "evidence": "not_required"})

    if evidence.get("artifactDigest") != expected_digest:
        return PolicyDecision(False, "security evidence does not describe the artifact being deployed", checks)
    checks["evidenceDigest"] = "pass"

    # A verdict already recorded against this evidence is binding: re-evaluating must
    # never turn a stored deny into an allow.
    recorded = evidence.get("decision")
    if recorded is not None and recorded != "allow":
        reason = evidence.get("reason")
        return PolicyDecision(
            False,
            str(reason) if reason else "a previous policy evaluation denied this artifact",
            {**checks, "recordedDecision": "deny"},
        )

    sbom = evidence.get("sbom")
    if not isinstance(sbom, dict) or sbom.get("generatedBy") != "syft" or not sbom.get("location"):
        return PolicyDecision(False, "SBOM evidence must be generated by Syft and include a location", checks)
    checks["sbom"] = "pass"

    scan = evidence.get("vulnerabilityScan")
    if not isinstance(scan, dict) or scan.get("scanner") != "trivy":
        return PolicyDecision(False, "vulnerability scan evidence must come from Trivy", checks)
    try:
        critical = int(scan.get("critical", 0))
        high = int(scan.get("high", 0))
    except (TypeError, ValueError):
        return PolicyDecision(False, "Trivy vulnerability counts are not numeric", checks)
    if critical > 0 or high > 0:
        return PolicyDecision(
            False, f"artifact has {critical} critical and {high} high vulnerabilities", {**checks, "vulnerabilityScan": "fail"}
        )
    if scan.get("status") != "passed":
        return PolicyDecision(False, "Trivy vulnerability scan did not pass", {**checks, "vulnerabilityScan": "fail"})
    checks["vulnerabilityScan"] = "pass"

    signature = evidence.get("signature")
    if not isinstance(signature, dict) or signature.get("provider") != "cosign" or signature.get("verified") is not True:
        return PolicyDecision(False, "Cosign signature evidence is missing or unverified", checks)
    checks["signature"] = "pass"

    return PolicyDecision(True, "artifact satisfies the netCI supply-chain policy", checks)
