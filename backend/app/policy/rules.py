from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

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


def require_team_access(
    owner_team: str | None,
    caller_teams: frozenset[str] | set[str],
    *,
    is_platform_admin: bool = False,
    require_owner: bool = False,
) -> None:
    """Whether this caller may act on an application owned by this team.

    Roles are global: they say what kind of thing someone may do. Ownership says which
    applications they may do it to. Without this, a `developer` role lets anyone run any
    team's pipeline and a `reviewer` role lets anyone approve any team's production
    release -- which is fine for one team and wrong for an organisation.

    An unowned application is unrestricted by default, so adopting ownership does not
    break applications that predate it. `require_owner` (NETCI_REQUIRE_APPLICATION_OWNER)
    closes that door once every application has an owner: the door is left open only for
    as long as the migration needs it.
    """

    if is_platform_admin:
        return
    if not owner_team:
        if require_owner:
            raise PolicyViolation(
                "this application has no owning team, and NETCI_REQUIRE_APPLICATION_OWNER "
                "is set; a platform-admin must assign one before it can be used"
            )
        return
    if owner_team not in caller_teams:
        held = ", ".join(sorted(caller_teams)) or "no teams"
        raise PolicyViolation(
            f"this application is owned by {owner_team!r}; you belong to {held}"
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


# ------------------------------------------------------------------ vulnerability waivers


class PolicyConfigurationError(Exception):
    """The exception file exists but cannot be trusted, so nothing may rely on it."""


@dataclass(frozen=True)
class VulnerabilityException:
    """A time-boxed, owned permission to ship a specific finding on a specific artifact.

    Every field is load-bearing. Without `expires` a waiver becomes permanent and the gate
    quietly stops meaning anything; without `owner` there is nobody to chase; without
    `cve` and `artifactDigest` it is a blanket exemption rather than an exception.
    """

    cve: str
    artifact_digest: str
    owner: str
    expires: date
    reason: str
    approved_by: str

    def covers(self, cve: str, digest: str, *, today: date) -> bool:
        return (
            self.cve.upper() == cve.upper()
            and self.artifact_digest == digest
            and today <= self.expires
        )

    def as_json(self) -> dict[str, object]:
        return {
            "cve": self.cve,
            "artifactDigest": self.artifact_digest,
            "owner": self.owner,
            "expires": self.expires.isoformat(),
            "reason": self.reason,
            "approvedBy": self.approved_by,
        }


def _require(entry: dict[str, Any], key: str, source: str) -> str:
    value = str(entry.get(key, "")).strip()
    if not value:
        raise PolicyConfigurationError(f"{source}: '{key}' is required")
    return value


def load_vulnerability_exceptions(path: Path | None = None) -> tuple[VulnerabilityException, ...]:
    """Read the exception register, or return nothing if there is none.

    Parsing is strict and fails closed: a malformed register raises rather than being
    skipped, because "the waiver file was invalid so we ignored it" and "the waiver file
    was invalid so we shipped anyway" must never be the same outcome.
    """

    location = path or (
        Path(os.getenv("NETCI_SECURITY_EXCEPTIONS_FILE", "")) if os.getenv("NETCI_SECURITY_EXCEPTIONS_FILE") else None
    )
    if location is None or not location.is_file():
        return ()

    import yaml

    try:
        payload = yaml.safe_load(location.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise PolicyConfigurationError(f"cannot read {location}: {exc}") from exc
    entries = payload.get("exceptions", [])
    if not isinstance(entries, list):
        raise PolicyConfigurationError(f"{location}: 'exceptions' must be a list")

    parsed: list[VulnerabilityException] = []
    for index, entry in enumerate(entries):
        source = f"{location}: exceptions[{index}]"
        if not isinstance(entry, dict):
            raise PolicyConfigurationError(f"{source}: must be a mapping")
        digest = _require(entry, "artifactDigest", source)
        if not IMMUTABLE_DIGEST.fullmatch(digest):
            raise PolicyConfigurationError(
                f"{source}: artifactDigest must be an immutable sha256 digest, so a waiver "
                "cannot follow a mutable tag onto a different image"
            )
        raw_expiry = entry.get("expires")
        if isinstance(raw_expiry, date) and not isinstance(raw_expiry, datetime):
            expires = raw_expiry
        else:
            try:
                expires = date.fromisoformat(str(raw_expiry))
            except ValueError as exc:
                raise PolicyConfigurationError(f"{source}: 'expires' must be a YYYY-MM-DD date") from exc
        parsed.append(
            VulnerabilityException(
                cve=_require(entry, "cve", source),
                artifact_digest=digest,
                owner=_require(entry, "owner", source),
                expires=expires,
                reason=_require(entry, "reason", source),
                approved_by=_require(entry, "approvedBy", source),
            )
        )
    return tuple(parsed)


def applicable_exceptions(
    exceptions: tuple[VulnerabilityException, ...],
    findings: list[str],
    digest: str,
    *,
    today: date | None = None,
) -> tuple[list[str], list[dict[str, object]]]:
    """Split blocking findings from waived ones, and say which waiver covered what.

    Returns `(still_blocking, waivers_used)`. An expired waiver covers nothing, which is
    the point of the expiry: the finding comes back on its own rather than needing someone
    to remember to remove the entry.
    """

    now = today or datetime.now(timezone.utc).date()
    blocking: list[str] = []
    used: list[dict[str, object]] = []
    for cve in findings:
        waiver = next((item for item in exceptions if item.covers(cve, digest, today=now)), None)
        if waiver is None:
            blocking.append(cve)
        else:
            used.append(waiver.as_json())
    return blocking, used


def _finding_ids(scan: dict[str, object]) -> list[str]:
    """The CVE identifiers the scan reported, from whichever shape the evidence uses.

    A waiver names a specific CVE, so a scan that only reports counts cannot be waived --
    there would be nothing to match against, and "3 highs" is not a thing anyone can
    take responsibility for.
    """

    findings = scan.get("findings")
    if isinstance(findings, list):
        identifiers = []
        for item in findings:
            if isinstance(item, str):
                identifiers.append(item)
            elif isinstance(item, dict) and item.get("id"):
                identifiers.append(str(item["id"]))
        return identifiers
    return []


IMMUTABLE_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


def evaluate_artifact_evidence(
    evidence: dict[str, object] | None,
    *,
    expected_digest: str | None,
    require_evidence: bool,
    exceptions: tuple[VulnerabilityException, ...] | None = None,
    today: date | None = None,
) -> PolicyDecision:
    """Decide whether an artifact may be deployed, from its CI evidence alone.

    Fail-closed in both directions that matter: evidence that is present must pass
    every check, and missing evidence is refused wherever evidence is required.
    Nothing here trusts a status string the pipeline merely asserted -- each field
    names the tool that produced it.
    """

    checks: dict[str, str] = {}
    waived_reason = ""

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
        # A finding may be waived only by a named owner, for this exact digest, until a
        # date that has not passed. Everything else still blocks. Without this path the
        # only way to ship a known-but-unfixable finding is to disable the gate for
        # everyone, which is how a supply-chain control dies in a real organisation.
        findings = _finding_ids(scan)
        waivers = exceptions if exceptions is not None else load_vulnerability_exceptions()
        blocked = f"artifact has {critical} critical and {high} high vulnerabilities"
        if not waivers:
            # The ordinary case: no exception register, so there is nothing to check the
            # findings against and no reason to talk about identifiers.
            return PolicyDecision(False, blocked, {**checks, "vulnerabilityScan": "fail"})
        if not findings:
            return PolicyDecision(
                False,
                f"{blocked}, and the scan did not list their identifiers, so no exception "
                "can apply to them",
                {**checks, "vulnerabilityScan": "fail"},
            )
        if len(findings) != critical + high:
            return PolicyDecision(
                False,
                f"{blocked} but the scan listed {len(findings)} identifiers; "
                "the counts and the findings must agree",
                {**checks, "vulnerabilityScan": "fail"},
            )
        blocking, used = applicable_exceptions(waivers, findings, expected_digest, today=today)
        if blocking:
            return PolicyDecision(
                False,
                f"{blocked}; no current exception covers {', '.join(sorted(blocking))}",
                {**checks, "vulnerabilityScan": "fail"},
            )
        # Deliberately not "pass": the artifact ships with known findings, and the reason
        # it was allowed has to survive into the audit trail and the deployment record.
        #
        # Recorded and carried, never returned from here. Returning an allow at this point
        # would skip every check below it -- an early version of this did exactly that, and
        # a waived CVE was enough to let an *unsigned* artifact through. A waiver applies
        # to the vulnerability gate and to nothing else.
        checks["vulnerabilityScan"] = "waived"
        checks["exceptions"] = ", ".join(str(item["cve"]) for item in used)
        waived_reason = "artifact ships with waived vulnerabilities: " + "; ".join(
            f"{item['cve']} until {item['expires']} (owner {item['owner']}, approved by {item['approvedBy']})"
            for item in used
        )
    elif scan.get("status") != "passed":
        return PolicyDecision(False, "Trivy vulnerability scan did not pass", {**checks, "vulnerabilityScan": "fail"})
    else:
        checks["vulnerabilityScan"] = "pass"

    signature = evidence.get("signature")
    if not isinstance(signature, dict) or signature.get("provider") != "cosign" or signature.get("verified") is not True:
        return PolicyDecision(False, "Cosign signature evidence is missing or unverified", checks)
    checks["signature"] = "pass"

    return PolicyDecision(True, waived_reason or "artifact satisfies the netCI supply-chain policy", checks)
