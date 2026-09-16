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

    def set_canary_rules(
        self,
        application_id: str,
        environment: str,
        rules: dict[str, Any],
    ) -> dict[str, Any]:
        """Configure L7 header/cookie matching rules for canary traffic steering."""
        key = (str(application_id), str(environment))
        if key not in self._routes:
            self._routes[key] = {
                "applicationId": str(application_id),
                "environment": str(environment),
                "canaryWeight": 0,
                "baselineWeight": 100,
                "activeColor": "blue",
                "canaryRules": {},
                "updatedAt": datetime.now(timezone.utc).isoformat(),
            }
        self._routes[key]["canaryRules"] = rules
        self._routes[key]["updatedAt"] = datetime.now(timezone.utc).isoformat()
        logger.info("Updated L7 canary rules for %s:%s -> %s", application_id, environment, rules)
        return dict(self._routes[key])

    def match_route(
        self,
        application_id: str,
        environment: str,
        headers: dict[str, str] | None = None,
        cookies: dict[str, str] | None = None,
    ) -> str:
        """Evaluate L7 rules to determine whether request targets 'canary' or 'baseline'."""
        key = (str(application_id), str(environment))
        route = self._routes.get(key, {})
        rules = route.get("canaryRules", {})
        if rules:
            # Check header rule
            hdr_name = rules.get("header_name") or rules.get("headerName")
            hdr_val = rules.get("header_value") or rules.get("headerValue")
            if hdr_name and headers:
                matched_hdr = next(
                    (v for k, v in headers.items() if k.lower() == hdr_name.lower()),
                    None,
                )
                if matched_hdr is not None and (not hdr_val or matched_hdr == hdr_val):
                    return "canary"

            # Check cookie rule
            cookie_rule = rules.get("cookie")
            if cookie_rule and cookies:
                c_name, _, c_expected = cookie_rule.partition("=")
                c_actual = cookies.get(c_name.strip())
                if c_actual is not None and (not c_expected or c_actual == c_expected.strip()):
                    return "canary"

        # Fallback to weight-based routing
        canary_weight = route.get("canaryWeight", 0)
        return "canary" if canary_weight >= 100 else "baseline"

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
                "canaryRules": {},
                "updatedAt": datetime.now(timezone.utc).isoformat(),
            }
        status = dict(self._routes[key])
        status.setdefault("canaryRules", {})
        return status


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


class TrafficRoutingUnavailable(RuntimeError):
    """No real traffic router is configured. Weights would be recorded and route nothing."""


class UnconfiguredTrafficRouter(TrafficRoutingAdapter):
    """Fail closed: outside local mode, canary weights need something that moves traffic.

    The in-memory adapter used to be the runtime default. A production API then answered
    `POST /deployments/{id}/traffic` with 200, stored the weight in a dict, and routed
    nothing -- a canary that reported 10 % while every request still hit the old release.
    """

    mode = "none"

    def _refuse(self) -> None:
        raise TrafficRoutingUnavailable(
            "no traffic router is configured (NETCI_TRAFFIC_ROUTER); canary and blue/green "
            "weights cannot be applied, so they are refused rather than recorded"
        )

    def set_traffic_weight(self, application_id, environment, canary_weight, baseline_weight=None):
        self._refuse()

    def switch_route(self, application_id, environment, active_color):
        self._refuse()

    def get_routing_status(self, application_id, environment):
        return {"applicationId": application_id, "environment": environment, "status": "not_configured", "router": "none"}

    def set_canary_rules(self, application_id, environment, rules):
        self._refuse()


def build_traffic_router() -> TrafficRoutingAdapter:
    """`memory` is local-only; unset outside local mode fails closed."""

    import os

    from .runtime_environment import is_local_runtime

    mode = os.getenv("NETCI_TRAFFIC_ROUTER", "").strip().lower()
    if mode == "memory":
        if not is_local_runtime():
            raise ValueError("NETCI_TRAFFIC_ROUTER=memory records weights without routing traffic; it is local-only")
        return InMemoryTrafficRoutingAdapter()
    if mode == "nginx-ingress":
        from .adapters.nginx_ingress_traffic import build_nginx_ingress_router

        return build_nginx_ingress_router()
    if mode in {"", "none"}:
        return InMemoryTrafficRoutingAdapter() if (mode == "" and is_local_runtime()) else UnconfiguredTrafficRouter()
    raise ValueError(f"NETCI_TRAFFIC_ROUTER must be memory, nginx-ingress or none (got {mode!r})")


# The runtime router. Built once, from configuration; never a fake outside local mode.
default_traffic_router = build_traffic_router()
