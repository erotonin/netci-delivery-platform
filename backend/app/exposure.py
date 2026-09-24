"""Vulnerability exposure and SBOM rescanning for running artifacts (ADR-045).

Answers "where does vulnerability X run right now?" by correlating:
1. Which artifacts are serving in each environment right now (from delivery state).
2. The CycloneDX SBOMs recorded for those immutable artifact digests.
3. Vulnerability findings from build-time CI and platform-side Trivy rescans.
4. Honest coverage metrics so an operator can distinguish "clean" from "not covered".
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from .adapters.trivy_rescan import SbomScanError, SbomScanner
from .domain.models import Environment
from .store.records import ArtifactFindingRecord, ArtifactRescanRecord

SEVERITY_ORDER = {
    "CRITICAL": 4,
    "HIGH": 3,
    "MEDIUM": 2,
    "LOW": 1,
    "UNKNOWN": 0,
}


@dataclass(frozen=True)
class InService:
    module_id: str
    system_id: str
    application_id: UUID
    environment: Environment
    artifact_digest: str
    pipeline_run_id: UUID


def in_service_artifacts(platform: Any, *, application_ids: set[UUID] | None = None) -> list[InService]:
    """Find all artifacts currently serving across modules and environments."""

    with platform.transaction() as tx:
        modules = tx.portal_modules()

    items: list[InService] = []
    for module in modules:
        if module.application_id is None:
            continue
        if application_ids is not None and module.application_id not in application_ids:
            continue
        for env in Environment:
            run = platform.source_run_in_service(module.application_id, env)
            if run and run.artifact_digest:
                items.append(
                    InService(
                        module_id=module.id,
                        system_id=module.system_id,
                        application_id=module.application_id,
                        environment=env,
                        artifact_digest=run.artifact_digest,
                        pipeline_run_id=run.id,
                    )
                )
    return items


def exposure(
    platform: Any,
    *,
    vulnerability_id: str | None = None,
    min_severity: str | None = None,
    application_ids: set[UUID] | None = None,
) -> dict[str, Any]:
    """Query which running modules are affected by vulnerabilities, with coverage reporting."""

    items = in_service_artifacts(platform, application_ids=application_ids)
    digests = list({item.artifact_digest for item in items})

    with platform.transaction() as tx:
        findings = tx.artifact_findings(digests, vulnerability_id)
        with_sbom = tx.artifact_sbom_digests(digests)
        rescans = tx.artifact_rescans(digests)

    # Filter findings by min_severity if given
    if min_severity is not None:
        target_rank = SEVERITY_ORDER.get(min_severity.upper(), 0)
        findings = tuple(
            f for f in findings if SEVERITY_ORDER.get(f.severity.upper(), 0) >= target_rank
        )

    # Group and merge findings by (artifact_digest, vulnerability_id, package, installed_version)
    grouped: dict[tuple[str, str, str, str], list[ArtifactFindingRecord]] = {}
    for f in findings:
        group_key = (f.artifact_digest, f.vulnerability_id, f.package, f.installed_version)
        grouped.setdefault(group_key, []).append(f)

    # Merged findings per artifact_digest
    digest_findings: dict[str, list[dict[str, Any]]] = {}
    for (digest, vid, pkg, inst_ver), group in grouped.items():
        sources = sorted(list({f.source for f in group}))
        best_severity = max(group, key=lambda f: SEVERITY_ORDER.get(f.severity.upper(), 0)).severity
        fixed_version = next((f.fixed_version for f in group if f.fixed_version), "")
        earliest_seen = min(f.first_seen_at for f in group)

        merged = {
            "vulnerabilityId": vid,
            "severity": best_severity,
            "package": pkg,
            "installedVersion": inst_ver,
            "fixedVersion": fixed_version,
            "sources": sources,
            "firstSeenAt": earliest_seen.isoformat(),
        }
        digest_findings.setdefault(digest, []).append(merged)

    # Build affected list for each in-service module
    affected: list[dict[str, Any]] = []
    for item in items:
        for f in digest_findings.get(item.artifact_digest, []):
            affected.append(
                {
                    "moduleId": item.module_id,
                    "systemId": item.system_id,
                    "environment": item.environment.value if hasattr(item.environment, "value") else str(item.environment),
                    "artifactDigest": item.artifact_digest,
                    "pipelineRunId": str(item.pipeline_run_id),
                    "vulnerabilityId": f["vulnerabilityId"],
                    "severity": f["severity"],
                    "package": f["package"],
                    "installedVersion": f["installedVersion"],
                    "fixedVersion": f["fixedVersion"],
                    "sources": f["sources"],
                    "firstSeenAt": f["firstSeenAt"],
                }
            )

    # Sort affected by (-severity rank, moduleId, environment)
    affected.sort(
        key=lambda a: (
            -SEVERITY_ORDER.get(a["severity"].upper(), 0),
            a["moduleId"],
            a["environment"],
        )
    )

    # Honest coverage metrics
    not_covered: list[dict[str, Any]] = []
    rescanned_count = 0
    rescan_failed_count = 0
    scanned_dates: list[datetime] = []

    for item in items:
        digest = item.artifact_digest
        rescan = rescans.get(digest)
        env_val = item.environment.value if hasattr(item.environment, "value") else str(item.environment)

        if digest not in with_sbom:
            reason = "no SBOM recorded"
        elif rescan is None:
            reason = "never rescanned by netCI"
        elif rescan.status == "failed":
            reason = f"last rescan failed: {rescan.detail}"
        else:
            reason = None

        if reason is not None:
            not_covered.append(
                {
                    "moduleId": item.module_id,
                    "environment": env_val,
                    "artifactDigest": digest,
                    "reason": reason,
                }
            )

        if rescan is not None:
            if rescan.status == "scanned":
                rescanned_count += 1
                scanned_dates.append(rescan.scanned_at)
            elif rescan.status == "failed":
                rescan_failed_count += 1

    oldest_rescan_at = min(scanned_dates).isoformat() if scanned_dates else None

    coverage = {
        "inService": len(items),
        "withSbom": sum(1 for item in items if item.artifact_digest in with_sbom),
        "rescanned": rescanned_count,
        "rescanFailed": rescan_failed_count,
        "oldestRescanAt": oldest_rescan_at,
        "notCovered": not_covered,
    }

    return {
        "vulnerabilityId": vulnerability_id,
        "minSeverity": min_severity,
        "affected": affected,
        "coverage": coverage,
    }


def rescan_in_service(
    platform: Any,
    scanner: SbomScanner,
    *,
    now: datetime | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """Rescan SBOMs of in-service artifacts using the configured scanner."""

    if now is None:
        now = datetime.now(timezone.utc)

    items = in_service_artifacts(platform)
    seen: set[str] = set()
    unique_digests: list[str] = []
    for item in items:
        if item.artifact_digest not in seen:
            seen.add(item.artifact_digest)
            unique_digests.append(item.artifact_digest)

    digests = unique_digests[:limit]

    scanned: list[str] = []
    failed: list[dict[str, str]] = []
    skipped_no_sbom: list[str] = []

    for digest in digests:
        with platform.transaction() as tx:
            sbom = tx.artifact_sbom(digest)

        if sbom is None:
            skipped_no_sbom.append(digest)
            continue

        try:
            found = scanner.scan(sbom.document)
            finding_records = [
                ArtifactFindingRecord(
                    artifact_digest=digest,
                    source="rescan",
                    vulnerability_id=f.vulnerability_id,
                    severity=f.severity,
                    package=f.package,
                    installed_version=f.installed_version,
                    fixed_version=f.fixed_version,
                    first_seen_at=now,
                    last_seen_at=now,
                )
                for f in found
            ]
            with platform.transaction() as tx:
                tx.replace_artifact_findings(digest, "rescan", finding_records, now)
                tx.record_artifact_rescan(
                    ArtifactRescanRecord(
                        artifact_digest=digest,
                        scanned_at=now,
                        status="scanned",
                        scanner=scanner.name,
                        detail=f"{len(found)} finding(s)",
                    )
                )
            scanned.append(digest)
        except SbomScanError as exc:
            with platform.transaction() as tx:
                tx.record_artifact_rescan(
                    ArtifactRescanRecord(
                        artifact_digest=digest,
                        scanned_at=now,
                        status="failed",
                        scanner=scanner.name,
                        detail=str(exc),
                    )
                )
            failed.append({"artifactDigest": digest, "error": str(exc)})

    return {
        "scanned": scanned,
        "failed": failed,
        "skippedNoSbom": skipped_no_sbom,
        "scanner": scanner.name,
    }
