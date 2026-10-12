"""Adapter for querying Prometheus to perform post-deploy metrics verification.

Canary and post-deploy analysis historically trusted metrics that the caller provided,
treating 'no data' as healthy (error rate 0, p95 latency 0). That is a fragile assumption
and an unsafe control: if the metric pipeline breaks, if an agent fails to report, or if
a caller sends an empty payload, a broken deployment looks completely green.

netCI must read metrics directly from Prometheus itself, and 'no data' must be a failure,
never a pass. An empty window or an unreachable metrics source fails closed so that unobserved
code is never promoted to production.

Configuration is read from the environment:

    NETCI_PROMETHEUS_URL        Base URL of the Prometheus server (e.g. http://prometheus:9090).
                                When empty, metrics verification is unconfigured and queries
                                fail closed via UnconfiguredMetricsSource.
    NETCI_PROMETHEUS_TIMEOUT    Timeout in seconds for Prometheus queries (default 10).
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit, urlunsplit
import urllib.request

logger = logging.getLogger(__name__)


class MetricsUnavailable(RuntimeError):
    """Prometheus could not be reached, returned an error, or had insufficient data."""


def _strip_userinfo(url: str) -> str:
    """Remove user:password credentials from a URL for safe display in errors."""
    parts = urlsplit(str(url or "").strip())
    if parts.username is None and parts.password is None:
        return str(url or "").strip().rstrip("/")
    host = (parts.hostname or "") + (f":{parts.port}" if parts.port else "")
    return urlunsplit((parts.scheme, host, parts.path, parts.query, parts.fragment)).rstrip("/")


def _sanitize_message(message: str) -> str:
    """Strip any user:password credentials from URL strings in the message."""
    return re.sub(r"://([^:@\s]+):([^@\s]+)@", "://", str(message))


class PrometheusMetricsSource:
    """Queries an external Prometheus HTTP API for metric verification."""

    name = "prometheus"

    def __init__(self, base_url: str, *, timeout_seconds: float = 10.0) -> None:
        self.base_url = base_url.rstrip("/")
        self._safe_base_url = _strip_userinfo(self.base_url)
        self.timeout_seconds = timeout_seconds

    def query(self, promql: str) -> float | None:
        """Execute an instant PromQL query and return a single scalar/vector value or None."""
        params = urlencode({"query": promql})
        url = f"{self.base_url}/api/v1/query?{params}"

        try:
            with urllib.request.urlopen(url, timeout=self.timeout_seconds) as response:
                raw_body = response.read()
        except HTTPError as exc:
            msg = _sanitize_message(f"Prometheus query failed with HTTP {exc.code} from {self._safe_base_url}")
            raise MetricsUnavailable(msg) from None
        except URLError as exc:
            msg = _sanitize_message(f"Prometheus query failed from {self._safe_base_url}: {exc.reason}")
            raise MetricsUnavailable(msg) from None
        except TimeoutError:
            msg = _sanitize_message(f"Prometheus query timed out after {self.timeout_seconds}s from {self._safe_base_url}")
            raise MetricsUnavailable(msg) from None
        except OSError as exc:
            msg = _sanitize_message(f"Prometheus network error from {self._safe_base_url}: {exc}")
            raise MetricsUnavailable(msg) from None

        try:
            payload = json.loads(raw_body.decode("utf-8", errors="replace"))
        except (ValueError, TypeError, UnicodeDecodeError):
            raise MetricsUnavailable(
                f"Prometheus response from {self._safe_base_url} was not valid JSON"
            ) from None

        if not isinstance(payload, dict) or payload.get("status") != "success":
            err_msg = ""
            if isinstance(payload, dict):
                err_msg = payload.get("error") or payload.get("errorType") or ""
            detail = f": {err_msg}" if err_msg else ""
            msg = _sanitize_message(f"Prometheus query failed from {self._safe_base_url}{detail}")
            raise MetricsUnavailable(msg)

        data = payload.get("data")
        if not isinstance(data, dict):
            raise MetricsUnavailable(f"Prometheus response from {self._safe_base_url} missing 'data' field")

        result_type = data.get("resultType")
        if result_type not in ("vector", "scalar"):
            raise MetricsUnavailable(
                f"unexpected Prometheus resultType {result_type!r}; expected 'vector' or 'scalar'"
            )

        if result_type == "vector":
            results = data.get("result")
            if not isinstance(results, list):
                raise MetricsUnavailable(f"Prometheus vector result from {self._safe_base_url} is not a list")
            if len(results) == 0:
                return None
            if len(results) > 1:
                raise MetricsUnavailable(
                    f"query returned {len(results)} series; a verification query must reduce to one value (use sum/max)"
                )
            series = results[0]
            if not isinstance(series, dict) or "value" not in series:
                raise MetricsUnavailable(f"Prometheus vector result missing 'value' from {self._safe_base_url}")
            val_tuple = series["value"]
            if not isinstance(val_tuple, (list, tuple)) or len(val_tuple) < 2:
                raise MetricsUnavailable(f"Prometheus vector value is invalid from {self._safe_base_url}")
            raw_val = val_tuple[1]
        else:
            # scalar
            scalar_res = data.get("result")
            if not isinstance(scalar_res, (list, tuple)) or len(scalar_res) < 2:
                raise MetricsUnavailable(f"Prometheus scalar result missing value from {self._safe_base_url}")
            raw_val = scalar_res[1]

        try:
            val = float(raw_val)
        except (ValueError, TypeError):
            raise MetricsUnavailable(
                f"Prometheus returned non-numeric value {raw_val!r} from {self._safe_base_url}"
            ) from None

        if math.isnan(val):
            return None

        return val


class UnconfiguredMetricsSource:
    """Null source used when NETCI_PROMETHEUS_URL is not set.

    Fails closed: outside a configured environment, verification queries cannot
    be executed, so attempting to verify must fail rather than quietly succeeding.
    """

    name = "none"

    def query(self, promql: str) -> float | None:
        raise MetricsUnavailable("no metrics source is configured (NETCI_PROMETHEUS_URL is empty)")


def build_metrics_source() -> PrometheusMetricsSource | UnconfiguredMetricsSource:
    """Compose the metrics source from environment configuration."""
    url = os.getenv("NETCI_PROMETHEUS_URL", "").strip().rstrip("/")
    if not url:
        return UnconfiguredMetricsSource()
    if not (url.startswith("http://") or url.startswith("https://")):
        raise ValueError(
            f"NETCI_PROMETHEUS_URL must start with http:// or https:// (got {url!r})"
        )
    timeout_str = os.getenv("NETCI_PROMETHEUS_TIMEOUT", "10").strip() or "10"
    try:
        timeout = float(timeout_str)
    except ValueError as exc:
        raise ValueError(
            f"NETCI_PROMETHEUS_TIMEOUT must be a number of seconds (got {timeout_str!r})"
        ) from exc
    return PrometheusMetricsSource(url, timeout_seconds=timeout)
