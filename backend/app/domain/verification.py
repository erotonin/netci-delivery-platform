"""Post-deploy verification specification, query rendering, and sample evaluation.

Deployments need automated health verification against Prometheus metrics over a sliding
time window before advancing waves or completing progressive delivery.

This module is purely domain logic (no I/O, no network calls). It validates the verification
specification defined in a module's pipeline configuration, safely renders PromQL query
templates without vulnerability to label injection, and evaluates sampled metric series
against strict threshold boundaries.

Key invariants:
- 'No data' is never a pass: an empty sample set or a configured query that returns no
  datapoints across the entire window fails verification.
- Queries are strictly templated: only {release}, {environment}, and {track} placeholders
  are allowed, and their replacement values must be validated DNS-safe identifiers to prevent
  PromQL label injection.
- PromQL syntax braces (such as label matchers) are preserved by using string replacement
  rather than Python str.format.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
import re
from typing import Any

IDENTIFIER_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
ALLOWED_TOP_LEVEL_KEYS = {
    "queries",
    "maxErrorRate",
    "maxP95LatencyMs",
    "windowMinutes",
    "intervalSeconds",
    "environments",
}
ALLOWED_QUERY_NAMES = {"errorRate", "p95LatencyMs"}
ALLOWED_PLACEHOLDERS = {"release", "environment", "track"}
ALLOWED_ENVIRONMENTS = {"dev", "staging", "prod"}


class VerificationConfigError(ValueError):
    """Configuration for post-deploy verification is missing, invalid, or malformed."""


@dataclass(frozen=True)
class VerificationSpec:
    """Parsed and validated specification for verifying deployment health via Prometheus."""

    queries: Mapping[str, str]
    max_error_rate: float
    max_p95_latency_ms: float
    window_minutes: int
    interval_seconds: int
    environments: tuple[str, ...]

    def as_json(self) -> dict[str, Any]:
        """Convert specification to camelCase JSON dictionary for persistence or API responses."""
        return {
            "queries": dict(self.queries),
            "maxErrorRate": self.max_error_rate,
            "maxP95LatencyMs": self.max_p95_latency_ms,
            "windowMinutes": self.window_minutes,
            "intervalSeconds": self.interval_seconds,
            "environments": list(self.environments),
        }

    def applies_to(self, environment: str) -> bool:
        """Check whether verification applies to the given deployment environment."""
        return environment in self.environments


def parse_verification(raw: object) -> VerificationSpec | None:
    """Parse and validate raw verification configuration dictionary."""
    if raw is None:
        return None

    if not isinstance(raw, dict):
        raise VerificationConfigError("verification configuration must be a dict")

    unknown_keys = set(raw.keys()) - ALLOWED_TOP_LEVEL_KEYS
    if unknown_keys:
        raise VerificationConfigError(f"unknown verification field(s): {', '.join(sorted(unknown_keys))}")

    if "queries" not in raw:
        raise VerificationConfigError("missing queries in verification configuration")

    raw_queries = raw["queries"]
    if not isinstance(raw_queries, dict):
        raise VerificationConfigError("queries must be a dict")
    if not raw_queries:
        raise VerificationConfigError("queries must not be empty (at least one of errorRate or p95LatencyMs is required)")

    unknown_queries = set(raw_queries.keys()) - ALLOWED_QUERY_NAMES
    if unknown_queries:
        raise VerificationConfigError(f"queries contains unknown query: {', '.join(sorted(unknown_queries))}")

    for q_name, q_val in raw_queries.items():
        if not isinstance(q_val, str) or not q_val.strip():
            raise VerificationConfigError(f"queries.{q_name} must be a non-empty string")
        if len(q_val) > 2000:
            raise VerificationConfigError(f"queries.{q_name} exceeds maximum length of 2000 characters")
        for match in re.finditer(r"\{([^{}\"=]*)\}", q_val):
            placeholder = match.group(1)
            if placeholder not in ALLOWED_PLACEHOLDERS:
                raise VerificationConfigError(
                    f"queries.{q_name} contains unsupported placeholder {{{placeholder}}}; "
                    "only {release}, {environment}, {track} are allowed"
                )

    if "maxErrorRate" in raw:
        val = raw["maxErrorRate"]
        if isinstance(val, bool) or not isinstance(val, (int, float)) or not (0 < val <= 1):
            raise VerificationConfigError(f"maxErrorRate must be a number in (0, 1] (got {val!r})")
        max_error_rate = float(val)
    else:
        max_error_rate = 0.02

    if "maxP95LatencyMs" in raw:
        val = raw["maxP95LatencyMs"]
        if isinstance(val, bool) or not isinstance(val, (int, float)) or val <= 0:
            raise VerificationConfigError(f"maxP95LatencyMs must be a number > 0 (got {val!r})")
        max_p95_latency_ms = float(val)
    else:
        max_p95_latency_ms = 1000.0

    if "windowMinutes" in raw:
        val = raw["windowMinutes"]
        if isinstance(val, bool) or not isinstance(val, int) or not (1 <= val <= 60):
            raise VerificationConfigError(f"windowMinutes must be an int in 1..60 (got {val!r})")
        window_minutes = val
    else:
        window_minutes = 5

    if "intervalSeconds" in raw:
        val = raw["intervalSeconds"]
        if isinstance(val, bool) or not isinstance(val, int) or not (15 <= val <= 300):
            raise VerificationConfigError(f"intervalSeconds must be an int in 15..300 (got {val!r})")
        interval_seconds = val
    else:
        interval_seconds = 30

    if interval_seconds > window_minutes * 60:
        raise VerificationConfigError(
            f"intervalSeconds ({interval_seconds}s) cannot be longer than windowMinutes ({window_minutes}m = {window_minutes * 60}s)"
        )

    if "environments" in raw:
        val = raw["environments"]
        if not isinstance(val, list) or not val:
            raise VerificationConfigError("environments must be a non-empty list")
        for env in val:
            if not isinstance(env, str) or env not in ALLOWED_ENVIRONMENTS:
                raise VerificationConfigError(f"environments contains invalid value: {env!r}; must be one of dev, staging, prod")
        if len(val) != len(set(val)):
            raise VerificationConfigError("environments must contain distinct values")
        environments = tuple(val)
    else:
        environments = ("staging", "prod")

    return VerificationSpec(
        queries=dict(raw_queries),
        max_error_rate=max_error_rate,
        max_p95_latency_ms=max_p95_latency_ms,
        window_minutes=window_minutes,
        interval_seconds=interval_seconds,
        environments=environments,
    )


def render_query(template: str, *, release: str, environment: str, track: str) -> str:
    """Render a PromQL template replacing {release}, {environment}, {track}.

    Validates that each placeholder value matches standard identifier syntax to prevent
    PromQL injection into label matchers.
    """
    for name, val in [("release", release), ("environment", environment), ("track", track)]:
        # fullmatch, not match: `$` also matches before a trailing newline, and a newline
        # pasted into a PromQL label matcher is exactly what this check exists to stop.
        if not isinstance(val, str) or not IDENTIFIER_RE.fullmatch(val):
            raise VerificationConfigError(
                f"invalid {name} {val!r}: must match ^[a-z0-9]([a-z0-9-]{{0,61}}[a-z0-9])?$"
            )

    return (
        template.replace("{release}", release)
        .replace("{environment}", environment)
        .replace("{track}", track)
    )


@dataclass(frozen=True)
class Sample:
    """A point-in-time observation of error rate and latency metrics."""

    at: datetime
    error_rate: float | None
    p95_latency_ms: float | None


@dataclass(frozen=True)
class VerificationResult:
    """The aggregate outcome of evaluating metric samples over a verification window."""

    passed: bool
    reason: str
    samples: int


def evaluate(spec: VerificationSpec, samples: Sequence[Sample]) -> VerificationResult:
    """Evaluate metric samples over a verification window against the spec."""
    if not samples:
        return VerificationResult(
            passed=False,
            reason="no metric samples were taken",
            samples=0,
        )

    # For each configured query name, consider only samples where that value is not None;
    # if NONE of the samples has a value for a configured query -> failed
    if "errorRate" in spec.queries:
        if not any(s.error_rate is not None for s in samples):
            return VerificationResult(
                passed=False,
                reason="errorRate: Prometheus returned no data for the whole window",
                samples=len(samples),
            )

    if "p95LatencyMs" in spec.queries:
        if not any(s.p95_latency_ms is not None for s in samples):
            return VerificationResult(
                passed=False,
                reason="p95LatencyMs: Prometheus returned no data for the whole window",
                samples=len(samples),
            )

    # Any sample above its threshold -> failed
    for sample in samples:
        if "errorRate" in spec.queries and sample.error_rate is not None:
            if sample.error_rate > spec.max_error_rate:
                return VerificationResult(
                    passed=False,
                    reason=(
                        f"errorRate {sample.error_rate * 100:.2f}% exceeds threshold "
                        f"{spec.max_error_rate * 100:.2f}% at {sample.at.isoformat()}"
                    ),
                    samples=len(samples),
                )
        if "p95LatencyMs" in spec.queries and sample.p95_latency_ms is not None:
            if sample.p95_latency_ms > spec.max_p95_latency_ms:
                return VerificationResult(
                    passed=False,
                    reason=(
                        f"p95LatencyMs {sample.p95_latency_ms:.1f}ms exceeds threshold "
                        f"{spec.max_p95_latency_ms:.1f}ms at {sample.at.isoformat()}"
                    ),
                    samples=len(samples),
                )

    return VerificationResult(
        passed=True,
        reason=f"{len(samples)} sample(s) over the window within thresholds",
        samples=len(samples),
    )


def verdict_for_canary(
    spec: VerificationSpec,
    error_rate: float | None,
    p95: float | None,
) -> VerificationResult:
    """Evaluate a single point-in-time canary measurement against the verification spec."""
    now = datetime.now(timezone.utc)
    return evaluate(spec, [Sample(at=now, error_rate=error_rate, p95_latency_ms=p95)])
