"""The toolchain a build reports is judged once, at evidence time (ADR-056)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.policy.rules import check_toolchain, evaluate_artifact_evidence
from app.toolchain import declared

DIGEST = "sha256:" + "a" * 64


def _reported(**overrides):
    tools = declared()["tools"]
    at = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)
    report = {"syft": tools["syft"]["version"], "trivy": tools["trivy"]["version"],
              "cosign": tools["cosign"]["version"], "buildah": "1.39.3",
              "trivyDbUpdatedAt": (at - timedelta(hours=6)).isoformat(), "reportedAt": at.isoformat()}
    report.update(overrides)
    return report


def test_the_declared_tools_and_a_fresh_db_pass():
    assert check_toolchain(_reported()) == (True, "pass", "")


def test_an_undeclared_version_is_refused_and_named():
    allowed, value, reason = check_toolchain(_reported(trivy="0.69.0"))
    assert (allowed, value) == (False, "drift") and "trivy 0.69.0" in reason


def test_a_db_older_than_the_maximum_at_build_time_is_refused():
    at = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)
    allowed, value, _ = check_toolchain(_reported(trivyDbUpdatedAt=(at - timedelta(hours=200)).isoformat()))
    assert (allowed, value) == (False, "trivy-db-stale")


def test_the_age_is_the_build_s_not_today_s():
    # Built a year ago with a DB that was six hours old then: still fresh for that build.
    at = datetime(2025, 9, 26, 12, 0, tzinfo=timezone.utc)
    assert check_toolchain(_reported(trivyDbUpdatedAt=(at - timedelta(hours=6)).isoformat(),
                                     reportedAt=at.isoformat()))[0] is True


@pytest.mark.parametrize("report", [None, {"syft": None}])
def test_no_report_or_no_timestamps_is_refused_when_enforced(report):
    assert check_toolchain(report)[0] is False


def test_warn_mode_allows_and_says_why(monkeypatch):
    monkeypatch.setenv("NETCI_TOOLCHAIN_ENFORCE", "warn")
    allowed, value, reason = check_toolchain(_reported(trivy="0.69.0"))
    assert allowed and value == "drift" and reason


def test_only_the_evidence_time_evaluation_checks_tools():
    evidence = {"artifactDigest": DIGEST, "artifactRef": "reg/app@" + DIGEST,
                "sbom": {"generatedBy": "syft", "location": "x"},
                "vulnerabilityScan": {"scanner": "trivy", "status": "passed", "critical": 0, "high": 0},
                "signature": {"provider": "cosign", "verified": True}, "toolVersions": _reported(trivy="0.69.0")}
    later = evaluate_artifact_evidence(evidence, expected_digest=DIGEST, require_evidence=True, require_provenance=False, exceptions=())
    assert "toolchain" not in later.checks
    at_evidence = evaluate_artifact_evidence(evidence, expected_digest=DIGEST, require_evidence=True,
                                             require_provenance=False, exceptions=(), check_tools=True)
    assert at_evidence.checks.get("toolchain") == "drift"


def test_a_report_that_leaves_a_declared_tool_out_is_refused():
    # compare() skips absent tools; the verdict must not read "not reported" as "matches".
    report = _reported()
    del report["trivy"]
    allowed, value, reason = check_toolchain(report)
    assert (allowed, value) == (False, "unreported") and "trivy" in reason
