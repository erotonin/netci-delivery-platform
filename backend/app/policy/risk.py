"""Risk score calculation engine for release plans and deployment requests.

Risk scores range from 0 to 100:
- Low (0-39): Standard pipeline, low blast radius, full automated test coverage.
- Medium (40-69): Standard production deployment or moderate complexity.
- High (70-89): Multi-module production waves, reduced test automation, or active CVE waivers.
- Critical (90-100): High blast radius across multiple modules without automation tests, or elevated CVE density.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..domain.models import Environment


@dataclass(frozen=True)
class RiskAssessment:
    score: int
    level: str  # "low", "medium", "high", "critical"
    factors: dict[str, int]
    recommendations: list[str]


class RiskCalculator:
    """Computes transparent, deterministic risk scores based on request attributes."""

    @staticmethod
    def calculate(
        *,
        environment: Environment | str,
        module_count: int = 1,
        run_automation_tests: bool = True,
        active_exceptions_count: int = 0,
        rollback_strategy: str = "automatic",
        is_break_glass: bool = False,
        extra_metadata: dict[str, Any] | None = None,
    ) -> RiskAssessment:
        env_str = environment.value if isinstance(environment, Environment) else str(environment).lower()
        factors: dict[str, int] = {}
        recommendations: list[str] = []

        # 1. Environment baseline
        if env_str in ("production", "prod"):
            factors["environment"] = 40
        elif env_str in ("staging", "stage"):
            factors["environment"] = 15
        else:
            factors["environment"] = 0

        # 2. Multi-module blast radius
        if module_count > 1:
            wave_risk = min(30, 10 + (module_count - 1) * 5)
            factors["multi_module_wave"] = wave_risk
            recommendations.append(f"Deploying {module_count} modules in wave increases blast radius; verify DAG health.")
        else:
            factors["multi_module_wave"] = 0

        # 3. Test automation coverage
        if not run_automation_tests:
            factors["no_automation_tests"] = 25
            recommendations.append("Automation tests disabled; manual post-deployment verification is required.")
        else:
            factors["automation_tests"] = -5

        # 4. Security waivers / active CVE exceptions
        if active_exceptions_count > 0:
            exception_risk = min(30, active_exceptions_count * 10)
            factors["active_security_exceptions"] = exception_risk
            recommendations.append(f"{active_exceptions_count} security exception(s) active; ensure CVE remediation roadmap is tracked.")

        # 5. Rollback strategy
        if rollback_strategy.lower() in ("manual", "none"):
            factors["manual_rollback"] = 15
            recommendations.append("Manual rollback configured; recommend enabling automatic progressive rollback.")
        elif rollback_strategy.lower() in ("canary", "blue-green"):
            factors["progressive_rollback"] = -5

        # 6. Break-glass emergency marker
        if is_break_glass:
            factors["break_glass_bypass"] = 20
            recommendations.append("Break-glass procedure invoked; retrospective incident ticket audit required.")

        raw_score = sum(factors.values())
        score = max(0, min(100, raw_score))

        if score >= 90:
            level = "critical"
        elif score >= 70:
            level = "high"
        elif score >= 40:
            level = "medium"
        else:
            level = "low"

        return RiskAssessment(
            score=score,
            level=level,
            factors=factors,
            recommendations=recommendations,
        )
