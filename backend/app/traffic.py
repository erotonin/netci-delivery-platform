# backend/app/traffic.py
"""Traffic routing adapters and canary metrics analysis for progressive delivery."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CanaryAnalysisResult:
    allowed: bool
    reason: str
    error_rate: float
    p95_latency_ms: float
    evaluated_at: str


class TrafficRoutingAdapter(ABC):
    """Abstract interface for progressive delivery traffic control."""

    @abstractmethod
    def set_traffic_weight(
        self,
        application_id: str,
        environment: str,
        canary_weight: int,
        baseline_weight: int | None = None,
    ) -> dict[str, Any]:
        """Set the canary traffic percentage (0-100)."""
        pass

    @abstractmethod
    def switch_route(
        self,
        application_id: str,
        environment: str,
        active_color: str,
    ) -> dict[str, Any]:
        """Switch the active traffic target between blue and green."""
        pass

    @abstractmethod
    def get_routing_status(
        self,
        application_id: str,
        environment: str,
    ) -> dict[str, Any]:
        """Query current routing distribution."""
        pass


class InMemoryTrafficRoutingAdapter(TrafficRoutingAdapter):
    """Thread-safe in-memory traffic router for testing and local operation."""

    def __init__(self) -> None:
        self._routes: dict[tuple[str, str], dict[str, Any]] = {}

    def set_traffic_weight(
        self,
        application_id: str,
        environment: str,
        canary_weight: int,
        baseline_weight: int | None = None,
    ) -> dict[str, Any]:
        canary_weight = max(0, min(100, canary_weight))
        bw = 100 - canary_weight if baseline_weight is None else baseline_weight
        key = (str(application_id), str(environment))
        status = {
            "applicationId": str(application_id),
            "environment": str(environment),
            "canaryWeight": canary_weight,
            "baselineWeight": bw,
            "activeColor": self._routes.get(key, {}).get("activeColor", "blue"),
            "updatedAt": datetime.now(timezone.utc).isoformat(),
        }
        self._routes[key] = status
        logger.info("Updated traffic routing for %s:%s -> canary=%d%%, baseline=%d%%", application_id, environment, canary_weight, bw)
        return status

    def switch_route(
        self,
        application_id: str,
        environment: str,
        active_color: str,
    ) -> dict[str, Any]:
        active_color = active_color.lower()
        if active_color not in ("blue", "green"):
            raise ValueError(f"Invalid route color: {active_color}, must be 'blue' or 'green'")
        key = (str(application_id), str(environment))
        existing = self._routes.get(key, {})
        status = {
            "applicationId": str(application_id),
            "environment": str(environment),
            "canaryWeight": 0,
            "baselineWeight": 100,
            "activeColor": active_color,
            "updatedAt": datetime.now(timezone.utc).isoformat(),
        }
        self._routes[key] = status
        logger.info("Switched blue-green active route for %s:%s -> %s", application_id, environment, active_color)
        return status

    def get_routing_status(
        self,
        application_id: str,
        environment: str,
    ) -> dict[str, Any]:
        key = (str(application_id), str(environment))
        if key not in self._routes:
            return {
                "applicationId": str(application_id),
                "environment": str(environment),
                "canaryWeight": 0,
                "baselineWeight": 100,
                "activeColor": "blue",
                "updatedAt": datetime.now(timezone.utc).isoformat(),
            }
        return dict(self._routes[key])


class CanaryAnalyzer:
    """Evaluates canary health metrics before approving traffic promotion."""

    DEFAULT_MAX_ERROR_RATE = 0.02  # 2% error rate threshold
    DEFAULT_MAX_P95_LATENCY_MS = 1000.0  # 1000ms p95 latency threshold

    @classmethod
    def evaluate(
        cls,
        metrics: dict[str, float] | None,
        thresholds: dict[str, float] | None = None,
    ) -> CanaryAnalysisResult:
        now_str = datetime.now(timezone.utc).isoformat()
        metrics = metrics or {}
        thresholds = thresholds or {}

        max_error = thresholds.get("maxErrorRate", cls.DEFAULT_MAX_ERROR_RATE)
        max_p95 = thresholds.get("maxP95LatencyMs", cls.DEFAULT_MAX_P95_LATENCY_MS)

        error_rate = float(metrics.get("errorRate", 0.0))
        p95_latency = float(metrics.get("p95LatencyMs", 0.0))

        if error_rate > max_error:
            return CanaryAnalysisResult(
                allowed=False,
                reason=f"Canary error rate {error_rate * 100:.2f}% exceeds threshold {max_error * 100:.2f}%",
                error_rate=error_rate,
                p95_latency_ms=p95_latency,
                evaluated_at=now_str,
            )

        if p95_latency > max_p95:
            return CanaryAnalysisResult(
                allowed=False,
                reason=f"Canary p95 latency {p95_latency:.1f}ms exceeds threshold {max_p95:.1f}ms",
                error_rate=error_rate,
                p95_latency_ms=p95_latency,
                evaluated_at=now_str,
            )

        return CanaryAnalysisResult(
            allowed=True,
            reason="All canary health metrics within acceptable thresholds",
            error_rate=error_rate,
            p95_latency_ms=p95_latency,
            evaluated_at=now_str,
        )


# Global default traffic routing adapter
default_traffic_router = InMemoryTrafficRoutingAdapter()
