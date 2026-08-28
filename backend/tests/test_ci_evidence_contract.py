"""The evidence CI publishes must be the evidence the policy can act on.

`scripts/netci_callback.py` turns a Trivy report into the payload netCI stores, and
`evaluate_artifact_evidence` decides from that payload. They are written in different
places and changed by different people, so this test holds them to the same shape --
including the part that is easy to get wrong: a waiver names a CVE, so evidence that
reports only counts can never be waived, however well-formed it looks.
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

from app.policy.rules import VulnerabilityException, evaluate_artifact_evidence


ROOT = Path(__file__).resolve().parents[2]
DIGEST = "sha256:" + "c" * 64
TODAY = date(2026, 8, 28)


@pytest.fixture(scope="module")
def callback():
    """Load the CI-side script the same way a build agent runs it: as a standalone file."""

    spec = importlib.util.spec_from_file_location("netci_callback", ROOT / "scripts" / "netci_callback.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["netci_callback"] = module
    spec.loader.exec_module(module)
    return module


def trivy_report() -> dict:
    """A Trivy JSON report in the shape `trivy image --format json` actually emits."""

    return {
        "SchemaVersion": 2,
        "ArtifactName": "registry.local/netci/app@" + DIGEST,
        "Results": [
            {
                "Target": "app (alpine 3.19)",
                "Class": "os-pkgs",
                "Vulnerabilities": [
                    {
                        "VulnerabilityID": "CVE-2026-12345",
                        "PkgName": "libcrypto3",
                        "InstalledVersion": "3.1.4-r5",
                        "FixedVersion": "3.1.4-r6",
                        "Severity": "CRITICAL",
                    },
                    {
                        "VulnerabilityID": "CVE-2026-22222",
                        "PkgName": "busybox",
                        "InstalledVersion": "1.36.1-r15",
                        "FixedVersion": "1.36.1-r16",
                        "Severity": "HIGH",
                    },
                    # Duplicated across targets, as Trivy does for a package appearing twice.
                    {
                        "VulnerabilityID": "CVE-2026-22222",
                        "PkgName": "busybox",
                        "InstalledVersion": "1.36.1-r15",
                        "FixedVersion": "1.36.1-r16",
                        "Severity": "HIGH",
                    },
                    {"VulnerabilityID": "CVE-2026-33333", "PkgName": "zlib", "Severity": "MEDIUM"},
                ],
            }
        ],
    }


def evidence_from(callback, report: dict) -> dict:
    """Exactly the vulnerabilityScan block `command_evidence` builds."""

    counts = callback.trivy_counts(report)
    findings = callback.trivy_findings(report)
    return {
        "artifactDigest": DIGEST,
        "sbom": {"generatedBy": "syft", "location": "s3://sboms/app.json", "format": "cyclonedx-json"},
        "vulnerabilityScan": {
            "scanner": "trivy",
            "status": "passed" if counts["critical"] == 0 and counts["high"] == 0 else "failed",
            "critical": counts["critical"],
            "high": counts["high"],
            "medium": counts["medium"],
            "findings": findings,
        },
        "signature": {"provider": "cosign", "verified": True},
    }


def test_counts_and_findings_agree_when_trivy_repeats_a_cve(callback):
    """Trivy lists a CVE once per affected package; both sides must count it once."""

    report = trivy_report()
    counts = callback.trivy_counts(report)
    findings = callback.trivy_findings(report)

    # CVE-2026-22222 appears twice in the report and is one finding, not two.
    assert counts == {"critical": 1, "high": 1, "medium": 1}
    assert [item["id"] for item in findings] == ["CVE-2026-12345", "CVE-2026-22222"]
    assert findings[0]["fixedVersion"] == "3.1.4-r6"
    # The invariant the policy enforces, satisfied by construction rather than by luck.
    assert len(findings) == counts["critical"] + counts["high"]


def test_the_policy_denies_real_ci_evidence_that_has_blocking_findings(callback):
    decision = evaluate_artifact_evidence(
        evidence_from(callback, trivy_report()), expected_digest=DIGEST, require_evidence=True
    )
    assert not decision.allowed
    assert decision.reason == "artifact has 1 critical and 1 high vulnerabilities"


def test_a_waiver_can_actually_be_written_against_real_ci_evidence(callback):
    """The reason CI publishes identifiers at all: without them no exception can apply."""

    report = trivy_report()
    # Trivy counted CVE-2026-22222 twice; the register waives it once, and both counts clear.
    waivers = (
        VulnerabilityException(
            cve="CVE-2026-12345",
            artifact_digest=DIGEST,
            owner="dana@corp.example",
            expires=TODAY + timedelta(days=7),
            reason="fixed in libcrypto3 3.1.4-r6, rebuilding the golden base this week",
            approved_by="raj@corp.example",
        ),
        VulnerabilityException(
            cve="CVE-2026-22222",
            artifact_digest=DIGEST,
            owner="dana@corp.example",
            expires=TODAY + timedelta(days=7),
            reason="busybox shell is not present in the runtime image",
            approved_by="raj@corp.example",
        ),
    )
    decision = evaluate_artifact_evidence(
        evidence_from(callback, report),
        expected_digest=DIGEST,
        require_evidence=True,
        exceptions=waivers,
        today=TODAY,
    )
    assert decision.allowed
    assert decision.checks["vulnerabilityScan"] == "waived"
    assert "CVE-2026-12345" in decision.reason and "CVE-2026-22222" in decision.reason


def test_a_single_finding_waives_cleanly_end_to_end(callback):
    report = {
        "Results": [
            {
                "Vulnerabilities": [
                    {
                        "VulnerabilityID": "CVE-2026-12345",
                        "PkgName": "libcrypto3",
                        "InstalledVersion": "3.1.4-r5",
                        "FixedVersion": "3.1.4-r6",
                        "Severity": "CRITICAL",
                    }
                ]
            }
        ]
    }
    waiver = VulnerabilityException(
        cve="CVE-2026-12345",
        artifact_digest=DIGEST,
        owner="dana@corp.example",
        expires=TODAY + timedelta(days=7),
        reason="fixed upstream; golden base rebuild scheduled",
        approved_by="raj@corp.example",
    )
    decision = evaluate_artifact_evidence(
        evidence_from(callback, report),
        expected_digest=DIGEST,
        require_evidence=True,
        exceptions=(waiver,),
        today=TODAY,
    )
    assert decision.allowed
    assert decision.checks["vulnerabilityScan"] == "waived"
    assert "CVE-2026-12345" in decision.reason


def test_a_clean_scan_from_real_ci_evidence_passes(callback):
    decision = evaluate_artifact_evidence(
        evidence_from(callback, {"Results": []}), expected_digest=DIGEST, require_evidence=True
    )
    assert decision.allowed
    assert decision.checks["vulnerabilityScan"] == "pass"


# ------------------------------------------------ through the API, not around it


def test_the_api_stores_the_finding_identifiers_it_is_sent(callback):
    """Pydantic drops what it does not declare, and a dropped field fails silently.

    The policy tests above call `evaluate_artifact_evidence` directly, so they pass even
    when the API never stores the identifiers. This one goes through the real endpoint --
    the only place the omission is visible, and the reason every waiver would otherwise
    have quietly failed to apply in production.
    """

    from fastapi.testclient import TestClient

    import app.main as main

    client = TestClient(main.app)
    machine = {"Authorization": "Bearer netci-local-pipeline-key"}

    application = client.post(
        "/applications",
        json={
            "name": f"evidence-app-{DIGEST[7:15]}",
            "repositoryUrl": "https://git.example.com/team/app",
            "pipelineTemplate": "container-ci-cd-v1",
            "runtime": "docker",
        },
    ).json()
    run = client.post(
        f"/applications/{application['id']}/pipeline-runs",
        json={"commitSha": "abcdef1234567", "environment": "staging"},
    ).json()

    published = evidence_from(callback, trivy_report())
    response = client.post(
        f"/pipeline-runs/{run['id']}/security-evidence",
        headers=machine,
        json={
            "artifactDigest": DIGEST,
            "sbom": published["sbom"],
            "vulnerabilityScan": published["vulnerabilityScan"],
            "signature": published["signature"],
        },
    )
    assert response.status_code == 202, response.text
    assert response.json()["decision"] == "deny"

    stored = client.get(f"/pipeline-runs/{run['id']}/security-evidence", headers=machine).json()
    scan = stored["vulnerabilityScan"]
    identifiers = [item["id"] for item in scan["findings"]]

    assert identifiers == ["CVE-2026-12345", "CVE-2026-22222"]
    assert scan["findings"][0]["fixedVersion"] == "3.1.4-r6"
    # The invariant a waiver depends on, checked where it actually has to hold.
    assert len(scan["findings"]) == scan["critical"] + scan["high"]
