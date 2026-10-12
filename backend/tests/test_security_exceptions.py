"""Time-boxed exceptions to the vulnerability gate.

A supply-chain gate with no legitimate way through does not stay switched on. Sooner or
later a team needs to ship with a known finding -- an upstream fix that is not out yet, a
false positive, a vulnerability in a code path the service does not reach -- and if the
only option is to disable the gate, that is what will happen, for everyone, permanently.

So there is a way through, and it is narrow: a named owner, a named approver, a specific
CVE, a specific artifact digest, and a date after which it stops working by itself. These
tests are mostly about the ways it must *not* work.
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest
import yaml

from app.policy.rules import (
    PolicyConfigurationError,
    VulnerabilityException,
    evaluate_artifact_evidence,
    load_vulnerability_exceptions,
)


DIGEST = "sha256:" + "a" * 64
OTHER_DIGEST = "sha256:" + "b" * 64
TODAY = date(2026, 8, 28)


def evidence(*, findings: list[str], critical: int = 0, high: int = 0, digest: str = DIGEST) -> dict:
    return {
        "artifactDigest": digest,
        "sbom": {"generatedBy": "syft", "location": "s3://sboms/app.json"},
        "vulnerabilityScan": {
            "scanner": "trivy",
            "status": "failed" if findings else "passed",
            "critical": critical,
            "high": high,
            "findings": findings,
        },
        "signature": {"provider": "cosign", "verified": True},
    }


def waiver(**overrides) -> VulnerabilityException:
    values = {
        "cve": "CVE-2026-1111",
        "artifact_digest": DIGEST,
        "owner": "dana@corp.example",
        "expires": TODAY + timedelta(days=14),
        "reason": "upstream fix lands in 2.4.2; service does not reach the affected path",
        "approved_by": "raj@corp.example",
    }
    values.update(overrides)
    return VulnerabilityException(**values)


def evaluate(evidence_payload, exceptions=(), today=TODAY):
    return evaluate_artifact_evidence(
        evidence_payload, expected_digest=DIGEST, require_evidence=True, exceptions=exceptions, today=today
    )


# ------------------------------------------------------------------ the happy path


def test_a_current_waiver_lets_a_known_finding_through_and_says_who_owns_it():
    decision = evaluate(evidence(findings=["CVE-2026-1111"], high=1), (waiver(),))

    assert decision.allowed
    # "waived" and not "pass": the artifact ships with a known finding, and the record
    # has to say so rather than looking identical to a clean scan.
    assert decision.checks["vulnerabilityScan"] == "waived"
    assert "CVE-2026-1111" in decision.reason
    assert "dana@corp.example" in decision.reason
    assert "raj@corp.example" in decision.reason


def test_a_clean_scan_still_reads_as_pass_not_waived():
    decision = evaluate(evidence(findings=[]), (waiver(),))
    assert decision.allowed
    assert decision.checks["vulnerabilityScan"] == "pass"
    assert "exceptions" not in decision.checks


# ---------------------------------------------------------------- the narrow parts


def test_an_expired_waiver_covers_nothing():
    """The expiry is the whole mechanism: the finding returns without anyone acting."""

    stale = waiver(expires=TODAY - timedelta(days=1))
    decision = evaluate(evidence(findings=["CVE-2026-1111"], high=1), (stale,))

    assert not decision.allowed
    assert "CVE-2026-1111" in decision.reason


def test_a_waiver_does_not_follow_the_finding_onto_a_different_artifact():
    other = waiver(artifact_digest=OTHER_DIGEST)
    decision = evaluate(evidence(findings=["CVE-2026-1111"], high=1), (other,))
    assert not decision.allowed


def test_a_waiver_covers_only_the_cve_it_names():
    decision = evaluate(evidence(findings=["CVE-2026-9999"], critical=1), (waiver(),))
    assert not decision.allowed
    assert "CVE-2026-9999" in decision.reason


def test_one_waived_finding_does_not_carry_an_unwaived_one():
    decision = evaluate(
        evidence(findings=["CVE-2026-1111", "CVE-2026-2222"], high=1, critical=1), (waiver(),)
    )
    assert not decision.allowed
    assert "CVE-2026-2222" in decision.reason
    assert "CVE-2026-1111" not in decision.reason


def test_counts_without_identifiers_cannot_be_waived():
    """Nobody can take responsibility for "three highs"; a waiver names a CVE."""

    decision = evaluate(evidence(findings=[], high=3), (waiver(),))
    assert not decision.allowed
    assert "identifiers" in decision.reason


def test_the_ordinary_deny_does_not_mention_exceptions_when_none_are_configured():
    """Most denials have nothing to do with waivers; the message should not imply they do."""

    decision = evaluate(evidence(findings=[], high=2, critical=1), ())
    assert not decision.allowed
    assert decision.reason == "artifact has 1 critical and 2 high vulnerabilities"


def test_counts_that_disagree_with_the_findings_are_refused():
    """Under-reporting the list while the counts say more must not smuggle findings past."""

    decision = evaluate(evidence(findings=["CVE-2026-1111"], high=1, critical=4), (waiver(),))
    assert not decision.allowed
    assert "must agree" in decision.reason


def test_a_waiver_cannot_rescue_a_missing_signature_or_sbom():
    """Exceptions apply to the vulnerability gate only, never to the rest of the chain."""

    unsigned = evidence(findings=["CVE-2026-1111"], high=1)
    unsigned["signature"] = {"provider": "cosign", "verified": False}
    assert not evaluate(unsigned, (waiver(),)).allowed

    no_sbom = evidence(findings=["CVE-2026-1111"], high=1)
    del no_sbom["sbom"]
    assert not evaluate(no_sbom, (waiver(),)).allowed


def test_a_recorded_deny_still_wins_over_a_waiver():
    denied = evidence(findings=["CVE-2026-1111"], high=1)
    denied["decision"] = "deny"
    denied["reason"] = "denied at publish time"
    decision = evaluate(denied, (waiver(),))
    assert not decision.allowed
    assert decision.reason == "denied at publish time"


# ------------------------------------------------------------------ the register


def register(tmp_path: Path, entries: list[dict]) -> Path:
    path = tmp_path / "security-exceptions.yaml"
    path.write_text(yaml.safe_dump({"exceptions": entries}), encoding="utf-8")
    return path


def valid_entry(**overrides) -> dict:
    entry = {
        "cve": "CVE-2026-1111",
        "artifactDigest": DIGEST,
        "owner": "dana@corp.example",
        "expires": "2026-12-31",
        "reason": "upstream fix pending",
        "approvedBy": "raj@corp.example",
    }
    entry.update(overrides)
    return entry


def test_the_register_round_trips(tmp_path):
    loaded = load_vulnerability_exceptions(register(tmp_path, [valid_entry()]))
    assert len(loaded) == 1
    assert loaded[0].cve == "CVE-2026-1111"
    assert loaded[0].expires == date(2026, 12, 31)


def test_no_register_means_no_exceptions_not_an_error(tmp_path):
    assert load_vulnerability_exceptions(tmp_path / "absent.yaml") == ()


@pytest.mark.parametrize("missing", ["cve", "owner", "reason", "approvedBy"])
def test_every_field_that_makes_a_waiver_accountable_is_required(tmp_path, missing):
    entry = valid_entry()
    del entry[missing]
    with pytest.raises(PolicyConfigurationError, match=missing):
        load_vulnerability_exceptions(register(tmp_path, [entry]))


def test_a_waiver_must_name_an_immutable_digest_not_a_tag(tmp_path):
    """A waiver on a mutable tag would silently follow that tag onto a different image."""

    with pytest.raises(PolicyConfigurationError, match="immutable sha256"):
        load_vulnerability_exceptions(register(tmp_path, [valid_entry(artifactDigest="myapp:latest")]))


def test_a_waiver_without_a_usable_expiry_is_refused(tmp_path):
    with pytest.raises(PolicyConfigurationError, match="expires"):
        load_vulnerability_exceptions(register(tmp_path, [valid_entry(expires="whenever")]))


def test_a_malformed_register_fails_closed_rather_than_being_skipped(tmp_path):
    """"The file was invalid so we ignored it" must not become "so we shipped anyway"."""

    path = tmp_path / "security-exceptions.yaml"
    path.write_text("exceptions: {not: a-list}", encoding="utf-8")
    with pytest.raises(PolicyConfigurationError, match="must be a list"):
        load_vulnerability_exceptions(path)
