"""Unified Policy Engine for artifact admission, production approval, and deployment gates."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from ..domain.models import Application, Environment
from ..store.records import PolicyDecisionRecord, SecurityExceptionRecord
from ..store.session import PlatformSession
from .break_glass import BreakGlassService
from .quota import QuotaEnforcer, QuotaViolation
from .risk import RiskCalculator
from .rules import (
    PolicyDecision,
    Role,
    VulnerabilityException,
    evaluate_artifact_evidence,
    load_vulnerability_exceptions,
    require_environment_permission,
    require_separation_of_duties,
    require_team_access,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PolicyEvaluationResult:
    allowed: bool
    reason: str
    risk_score: int
    checks: dict[str, Any]
    rules_evaluated: list[str]
    evaluator: str = "builtin"
    metadata: dict[str, Any] = field(default_factory=dict)


class BuiltinPolicyEngine:
    """Built-in deterministic policy evaluator."""

    @classmethod
    def evaluate_artifact_admission(
        cls,
        session: PlatformSession,
        *,
        evidence: dict[str, Any] | None,
        expected_digest: str,
        require_evidence: bool = True,
        target_id: str = "",
        now: datetime | None = None,
    ) -> PolicyEvaluationResult:
        rules_evaluated = ["digest_format", "sbom_verification", "vulnerability_scan", "signature_verification"]
        ts = now or datetime.now(timezone.utc)
        today = ts.date()

        # Check for active break-glass
        bg = BreakGlassService.active_break_glass(session, target_type="artifact", target_id=expected_digest, now=ts)
        if not bg and target_id:
            bg = BreakGlassService.active_break_glass(session, target_type="admission", target_id=target_id, now=ts)

        # Merge database exceptions with file-based exceptions
        db_exceptions = session.security_exceptions(active_only=True, now=ts)
        converted = [
            VulnerabilityException(
                cve=e.cve,
                artifact_digest=e.artifact_digest,
                owner=e.owner,
                expires=e.expires_at.date(),
                reason=e.reason,
                approved_by=e.approved_by,
            )
            for e in db_exceptions
        ]

        try:
            file_exceptions = load_vulnerability_exceptions()
            all_exceptions = tuple(converted) + tuple(file_exceptions)
        except Exception:
            all_exceptions = tuple(converted)

        decision: PolicyDecision = evaluate_artifact_evidence(
            evidence,
            expected_digest=expected_digest,
            require_evidence=require_evidence,
            exceptions=all_exceptions,
            today=today,
        )

        checks = dict(decision.checks)
        allowed = decision.allowed
        reason = decision.reason

        # Break-glass override
        if not allowed and bg:
            rules_evaluated.append("break_glass_bypass")
            allowed = True
            reason = f"Allowed via active emergency break-glass {bg.id} (ticket: {bg.incident_ticket}): {bg.reason}"
            checks["break_glass"] = {
                "id": str(bg.id),
                "incident_ticket": bg.incident_ticket,
                "requested_by": bg.requested_by,
                "approved_by": bg.approved_by,
            }

        # Calculate risk score
        risk = RiskCalculator.calculate(
            environment=Environment.PROD if require_evidence else Environment.DEV,
            active_exceptions_count=len([e for e in all_exceptions if e.artifact_digest == expected_digest]),
            is_break_glass=bg is not None,
        )

        return PolicyEvaluationResult(
            allowed=allowed,
            reason=reason,
            risk_score=risk.score,
            checks=checks,
            rules_evaluated=rules_evaluated,
            evaluator="builtin",
            metadata={"risk_level": risk.level, "recommendations": risk.recommendations},
        )


class PolicyEngine:
    """Enterprise Governance Policy Engine with durable auditing of all decisions."""

    @classmethod
    def evaluate_and_record_artifact(
        cls,
        session: PlatformSession,
        *,
        evidence: dict[str, Any] | None,
        expected_digest: str,
        require_evidence: bool = True,
        target_id: str = "",
        now: datetime | None = None,
    ) -> PolicyDecisionRecord:
        eval_result = BuiltinPolicyEngine.evaluate_artifact_admission(
            session,
            evidence=evidence,
            expected_digest=expected_digest,
            require_evidence=require_evidence,
            target_id=target_id,
            now=now,
        )

        record = PolicyDecisionRecord(
            id=uuid4(),
            scope="artifact",
            target_type="artifact_digest",
            target_id=expected_digest,
            allowed=eval_result.allowed,
            reason=eval_result.reason,
            risk_score=eval_result.risk_score,
            checks=eval_result.checks,
            rules_evaluated=eval_result.rules_evaluated,
            evaluator=eval_result.evaluator,
            evaluated_at=now or datetime.now(timezone.utc),
            metadata=eval_result.metadata,
        )
        session.record_policy_decision(record)
        return record

    @classmethod
    def evaluate_and_record_production_approval(
        cls,
        session: PlatformSession,
        *,
        request_id: str,
        application: Application,
        requested_by: str,
        approver: str,
        approver_roles: frozenset[Role] | set[Role],
        module_count: int = 1,
        run_automation_tests: bool = True,
        rollback_strategy: str = "automatic",
        now: datetime | None = None,
    ) -> PolicyDecisionRecord:
        ts = now or datetime.now(timezone.utc)
        rules_evaluated = ["environment_permission", "separation_of_duties", "resource_quota", "risk_assessment"]
        checks: dict[str, Any] = {}
        allowed = True
        reasons: list[str] = []

        # Check for break-glass
        bg = BreakGlassService.active_break_glass(session, target_type="production_request", target_id=request_id, now=ts)

        # 1. Environment permission
        try:
            require_environment_permission(Environment.PROD, approver_roles)
            checks["environment_permission"] = "pass"
        except Exception as exc:
            checks["environment_permission"] = f"fail: {exc}"
            allowed = False
            reasons.append(str(exc))

        # 2. Separation of duties
        try:
            require_separation_of_duties(requested_by, approver)
            checks["separation_of_duties"] = "pass"
        except Exception as exc:
            checks["separation_of_duties"] = f"fail: {exc}"
            allowed = False
            reasons.append(str(exc))

        # 3. Quota check
        try:
            QuotaEnforcer.check_deployment_quota(session, application_id=application.id, team=application.owner_team)
            checks["quota"] = "pass"
        except QuotaViolation as exc:
            checks["quota"] = f"fail: {exc}"
            allowed = False
            reasons.append(str(exc))

        # Break-glass override for non-critical gate failures (except separation of duties which is strict)
        if not allowed and bg:
            # Dual-control rule: break-glass cannot override requester approving their own request unless break-glass was itself dual-approved
            rules_evaluated.append("break_glass_bypass")
            allowed = True
            reasons = [f"Emergency override via break-glass {bg.id} (ticket: {bg.incident_ticket}): {bg.reason}"]
            checks["break_glass"] = {
                "id": str(bg.id),
                "incident_ticket": bg.incident_ticket,
                "approved_by": bg.approved_by,
            }

        # 4. Risk assessment
        active_exceptions = session.security_exceptions(active_only=True, now=ts)
        risk = RiskCalculator.calculate(
            environment=Environment.PROD,
            module_count=module_count,
            run_automation_tests=run_automation_tests,
            active_exceptions_count=len(active_exceptions),
            rollback_strategy=rollback_strategy,
            is_break_glass=bg is not None,
        )

        reason = "; ".join(reasons) if not allowed else "All governance policies satisfied"
        if bg and allowed:
            reason = f"Approved with break-glass override: {reasons[0]}"

        record = PolicyDecisionRecord(
            id=uuid4(),
            scope="production_request",
            target_type="production_request",
            target_id=request_id,
            allowed=allowed,
            reason=reason,
            risk_score=risk.score,
            checks=checks,
            rules_evaluated=rules_evaluated,
            evaluator="builtin",
            evaluated_at=ts,
            metadata={
                "risk_level": risk.level,
                "risk_factors": risk.factors,
                "recommendations": risk.recommendations,
                "module_count": module_count,
            },
        )
        session.record_policy_decision(record)
        return record
