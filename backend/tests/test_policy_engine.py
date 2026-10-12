"""Tests for Phase 11 Enterprise Governance Policy Engine and Risk Scoring."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from uuid import uuid4

import pytest

from app.domain.models import Application, Environment, Runtime
from app.policy.break_glass import BreakGlassService
from app.policy.engine import BuiltinPolicyEngine, PolicyEngine
from app.policy.risk import RiskAssessment, RiskCalculator
from app.policy.rules import Role
from app.store.memory import InMemoryDatabase
from app.store.records import SecurityExceptionRecord

DIGEST = "sha256:" + "a" * 64


def test_risk_calculator_low_risk_dev():
    risk = RiskCalculator.calculate(
        environment=Environment.DEV,
        module_count=1,
        run_automation_tests=True,
        active_exceptions_count=0,
        rollback_strategy="automatic",
    )
    assert risk.score == 0
    assert risk.level == "low"


def test_risk_calculator_high_risk_production_multimodule_no_tests():
    risk = RiskCalculator.calculate(
        environment=Environment.PROD,
        module_count=4,
        run_automation_tests=False,
        active_exceptions_count=2,
        rollback_strategy="manual",
    )
    # 40 (prod) + 25 (4 modules) + 25 (no tests) + 20 (2 waivers) + 15 (manual rollback) = 125 -> capped at 100
    assert risk.score == 100
    assert risk.level == "critical"
    assert len(risk.recommendations) > 0


def test_policy_engine_evaluates_and_records_artifact():
    db = InMemoryDatabase()
    now = datetime.now(timezone.utc)
    evidence = {
        "artifactDigest": DIGEST,
        "sbom": {"generatedBy": "syft", "location": "s3://sboms/app.json"},
        "vulnerabilityScan": {"scanner": "trivy", "status": "passed", "critical": 0, "high": 0},
        "signature": {"provider": "cosign", "verified": True},
    }

    with db.transaction() as session:
        rec = PolicyEngine.evaluate_and_record_artifact(
            session,
            evidence=evidence,
            expected_digest=DIGEST,
            require_evidence=True,
        )
        assert rec.allowed is True
        assert rec.scope == "artifact"
        assert rec.target_id == DIGEST
        assert rec.checks.get("sbom") == "pass"
        assert rec.checks.get("digest") == "pass"

        # Check recorded decision in paginated query
        items, next_cur, has_more = session.policy_decisions_paginated(scope="artifact")
        assert len(items) == 1
        assert items[0].id == rec.id


def test_policy_engine_evaluates_production_approval_dual_control():
    db = InMemoryDatabase()
    app = Application(
        name="billing",
        repository_url="https://github.com/example/billing",
        pipeline_template="container-ci-cd-v1",
        runtime=Runtime.DOCKER,
        default_environment=Environment.PROD,
        stages=(),
        owner_team="finance",
        id=uuid4(),
        created_at=datetime.now(timezone.utc),
    )

    with db.transaction() as session:
        # 1. Requester cannot self-approve
        self_approval = PolicyEngine.evaluate_and_record_production_approval(
            session,
            request_id="PR-100",
            application=app,
            requested_by="alice@corp.example",
            approver="alice@corp.example",
            approver_roles=frozenset({Role.REVIEWER}),
        )
        assert self_approval.allowed is False
        assert "requester cannot approve" in self_approval.reason

        # 2. Reviewer with proper role approves
        valid_approval = PolicyEngine.evaluate_and_record_production_approval(
            session,
            request_id="PR-100",
            application=app,
            requested_by="alice@corp.example",
            approver="bob@corp.example",
            approver_roles=frozenset({Role.REVIEWER}),
        )
        assert valid_approval.allowed is True
        assert valid_approval.checks.get("separation_of_duties") == "pass"
        assert valid_approval.checks.get("environment_permission") == "pass"
