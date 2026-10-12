"""Service scorecards: a read-only projection over data netCI already stores.

Nothing here writes anything or calls out to a network. Each check names the fact it is
based on, and a check this projection cannot evaluate from that fact is `None` --
"unknown" -- never a pass. A module scorecard is truthful about what has and has not
been established for that module, same as the platform is asked to be about itself.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from ..delivery import DeliveryError
from ..domain.models import Environment, PipelineStatus
from ..exposure import exposure as compute_exposure

#: How stale an in-service artifact's last rescan may be before "rescanned-recently" fails.
RESCAN_FRESHNESS = timedelta(days=7)
#: The window "recent-success" and DORA's change-fail-rate look back over.
RECENT_SUCCESS_WINDOW = timedelta(days=30)
#: netCI's own bar for an acceptable production change-fail rate.
MAX_CHANGE_FAIL_RATE = 0.15


def _check(check_id: str, title: str, passed: bool | None, detail: str) -> dict[str, object]:
    return {"id": check_id, "title": title, "passed": passed, "detail": detail}


def _application(platform: Any, module: dict[str, object]):
    application_id = module.get("applicationId")
    if not application_id:
        return None
    try:
        return platform.get_application(UUID(str(application_id)))
    except DeliveryError:
        return None


def _security_evidence(platform: Any, pipeline_run_id: UUID) -> dict[str, object]:
    """`security_evidence` raises when a run published none; absence is not an error here."""

    try:
        return platform.security_evidence(pipeline_run_id)
    except DeliveryError:
        return {}


def _in_service_digests(platform: Any, application_id: UUID) -> set[str]:
    digests: set[str] = set()
    for environment in Environment:
        run = platform.source_run_in_service(application_id, environment)
        if run is not None and run.artifact_digest:
            digests.add(run.artifact_digest)
    return digests


def _check_owner(application) -> dict[str, object]:
    owner = application.owner_team if application else None
    if owner:
        return _check("owner", "Has an owner team", True, f"owned by {owner}")
    return _check("owner", "Has an owner team", False, "no owner team is set on the application")


def _check_delivery_rules(rules) -> dict[str, object]:
    if rules.defaulted:
        return _check(
            "delivery-rules", "Declares its own delivery rules", False,
            "using the default delivery rules; the module declares none of its own",
        )
    return _check("delivery-rules", "Declares its own delivery rules", True, "delivery rules are declared")


def _check_verification(verification_spec) -> dict[str, object]:
    if verification_spec is not None:
        return _check("verification", "Declares post-deploy verification", True,
                       "pipelineConfig.verification is declared")
    return _check("verification", "Declares post-deploy verification", False,
                  "pipelineConfig.verification is not declared")


def _check_staging_before_prod(module: dict[str, object], rules) -> dict[str, object]:
    title = "Production promotion requires a healthy staging deployment first"
    environments = module.get("deploymentEnvironments") or []
    has_prod = any(str(entry.get("environment")) == Environment.PROD.value for entry in environments)
    if not has_prod:
        return _check("staging-before-prod", title, None, "the module has no prod deployment target")
    prod_rule = rules.promotion.get(Environment.PROD)
    requires_staging = prod_rule is not None and prod_rule.require_healthy_in == Environment.STAGING
    if requires_staging:
        return _check("staging-before-prod", title, True, "prod promotion requires healthy in staging")
    return _check("staging-before-prod", title, False,
                  "prod promotion does not require a healthy staging deployment first")


def _check_provenance(platform: Any, runs) -> dict[str, object]:
    title = "Newest published build has verified provenance"
    candidates = [r for r in runs if r.status == PipelineStatus.SUCCEEDED and r.artifact_digest and r.publish_artifact]
    if not candidates:
        return _check("provenance", title, None, "no succeeded run has published an artifact")
    newest = max(candidates, key=lambda r: r.created_at)
    evidence = _security_evidence(platform, newest.id)
    provenance = evidence.get("provenance") if isinstance(evidence, dict) else None
    verified = bool(isinstance(provenance, dict) and provenance.get("verified"))
    if verified:
        return _check("provenance", title, True, f"run {newest.id} carries verified provenance")
    return _check("provenance", title, False, f"run {newest.id} has no verified provenance")


def _check_sbom_in_service(platform: Any, in_service: set[str]) -> dict[str, object]:
    title = "Every in-service artifact has a recorded SBOM"
    if not in_service:
        return _check("sbom-in-service", title, None, "nothing is currently in service")
    with platform.transaction() as tx:
        with_sbom = tx.artifact_sbom_digests(in_service)
    missing = in_service - with_sbom
    if not missing:
        return _check("sbom-in-service", title, True, "every in-service digest has an SBOM")
    return _check("sbom-in-service", title, False, f"{len(missing)} in-service digest(s) have no SBOM")


def _check_rescanned_recently(platform: Any, in_service: set[str], now: datetime) -> dict[str, object]:
    title = "Every in-service artifact was rescanned within 7 days"
    if not in_service:
        return _check("rescanned-recently", title, None, "nothing is currently in service")
    with platform.transaction() as tx:
        rescans = tx.artifact_rescans(in_service)
    cutoff = now - RESCAN_FRESHNESS
    stale = [
        digest for digest in in_service
        if (rescan := rescans.get(digest)) is None or rescan.status != "scanned" or rescan.scanned_at < cutoff
    ]
    if not stale:
        return _check("rescanned-recently", title, True, "every in-service digest was rescanned within 7 days")
    return _check("rescanned-recently", title, False,
                  f"{len(stale)} in-service digest(s) were not rescanned within 7 days")


def _check_no_critical_running(platform: Any, application_id: UUID) -> dict[str, object]:
    title = "No critical vulnerability is running"
    result = compute_exposure(platform, min_severity="CRITICAL", application_ids={application_id})
    coverage = result["coverage"]
    if coverage["inService"] == 0:
        return _check("no-critical-running", title, None, "nothing is currently in service")
    affected = result["affected"]
    not_covered = coverage.get("notCovered") or []
    if affected:
        ids = sorted({a["vulnerabilityId"] for a in affected})
        shown = ", ".join(ids[:3])
        suffix = f" (+{len(ids) - 3} more)" if len(ids) > 3 else ""
        passed, detail = False, f"critical vulnerabilities running: {shown}{suffix}"
    else:
        passed, detail = True, "no critical vulnerabilities are running"
    if not_covered:
        detail += f"; {len(not_covered)} in-service artifact(s) are not covered by scanning"
    return _check("no-critical-running", title, passed, detail)


def _check_recent_success(runs, now: datetime) -> dict[str, object]:
    title = "Succeeded at least once in the last 30 days"
    cutoff = now - RECENT_SUCCESS_WINDOW
    succeeded = [r for r in runs if r.status == PipelineStatus.SUCCEEDED and r.updated_at >= cutoff]
    if succeeded:
        return _check("recent-success", title, True, f"{len(succeeded)} succeeded run(s) in the last 30 days")
    return _check("recent-success", title, False, "no succeeded run in the last 30 days")


def _check_change_fail_rate(dora: dict[str, object]) -> dict[str, object]:
    title = "Production change-fail rate is below 15%"
    if not dora.get("deployment_frequency"):
        return _check("change-fail-rate", title, None, "no production deployments to measure")
    rate = float(dora.get("change_fail_rate") or 0.0)
    passed = rate < MAX_CHANGE_FAIL_RATE
    return _check("change-fail-rate", title, passed, f"change-fail rate is {rate:.0%}")


def module_scorecard(platform: Any, portal: Any, module_id: str, *, now: datetime) -> dict[str, object]:
    """Assemble one module's scorecard from facts netCI already has on hand.

    Every check says what it is based on in its `detail`; a check that this module has no
    basis for evaluating (nothing deployed yet, no production target, ...) is `None`, not a
    silent pass -- a fresh module must never look identical to a hardened one.
    """

    module = portal.module(module_id)
    application = _application(platform, module)
    application_id = application.id if application else None
    runs = platform.list_pipeline_runs(application_id) if application_id else ()
    rules = portal.delivery_rules(module_id)
    verification_spec = portal.verification_spec(module_id)
    dora = portal.dora(module_id)
    in_service = _in_service_digests(platform, application_id) if application_id else set()

    checks = [
        _check_owner(application),
        _check_delivery_rules(rules),
        _check_verification(verification_spec),
        _check_staging_before_prod(module, rules),
        _check_provenance(platform, runs),
        _check_sbom_in_service(platform, in_service),
        _check_rescanned_recently(platform, in_service, now),
        (
            _check_no_critical_running(platform, application_id)
            if application_id
            else _check("no-critical-running", "No critical vulnerability is running", None,
                        "module has no linked application")
        ),
        _check_recent_success(runs, now),
        _check_change_fail_rate(dora),
    ]

    passed = sum(1 for c in checks if c["passed"] is True)
    known = sum(1 for c in checks if c["passed"] is not None)
    return {
        "moduleId": module_id,
        "score": {"passed": passed, "known": known, "total": len(checks)},
        "checks": checks,
    }


def all_scorecards(platform: Any, portal: Any, module_ids: list[str], *, now: datetime) -> dict[str, object]:
    """Every visible module's scorecard, worst-first so the portal can lead with it."""

    items = []
    for module_id in module_ids:
        module = portal.module(module_id)
        scorecard = module_scorecard(platform, portal, module_id, now=now)
        score = scorecard["score"]
        ratio = (score["passed"] / score["known"]) if score["known"] else 0.0
        items.append((ratio, {
            "moduleId": module_id,
            "systemId": module.get("systemId"),
            "name": module.get("name"),
            "score": score,
        }))
    items.sort(key=lambda pair: (pair[0], pair[1]["moduleId"]))
    return {"items": [item for _, item in items]}
