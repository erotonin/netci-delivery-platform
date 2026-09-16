"""Prometheus metrics registry, exposition format generator, and ASGI middleware."""

from __future__ import annotations

import threading
import time
from collections import defaultdict
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

DEFAULT_LATENCY_BUCKETS = (
    0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0
)


def _format_labels(labels: dict[str, str]) -> str:
    if not labels:
        return ""
    pairs = [f'{k}="{v}"' for k, v in sorted(labels.items())]
    return "{" + ",".join(pairs) + "}"


class MetricsRegistry:
    """Thread-safe collector for counters, gauges, and histograms."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[str, dict[tuple[tuple[str, str], ...], float]] = defaultdict(
            lambda: defaultdict(float)
        )
        self._counter_help: dict[str, str] = {}

        self._gauges: dict[str, dict[tuple[tuple[str, str], ...], float]] = defaultdict(
            lambda: defaultdict(float)
        )
        self._gauge_help: dict[str, str] = {}

        self._histograms: dict[
            str,
            dict[
                tuple[tuple[str, str], ...],
                dict[str, Any],  # {"buckets": dict[float, int], "sum": float, "count": int}
            ],
        ] = defaultdict(dict)
        self._histogram_help: dict[str, str] = {}
        self._histogram_buckets: dict[str, tuple[float, ...]] = {}

    def register_counter(self, name: str, help_text: str) -> None:
        with self._lock:
            self._counter_help[name] = help_text

    def register_gauge(self, name: str, help_text: str) -> None:
        with self._lock:
            self._gauge_help[name] = help_text

    def register_histogram(
        self, name: str, help_text: str, buckets: tuple[float, ...] = DEFAULT_LATENCY_BUCKETS
    ) -> None:
        with self._lock:
            self._histogram_help[name] = help_text
            self._histogram_buckets[name] = tuple(sorted(buckets))

    def counter_inc(self, name: str, labels: dict[str, str] | None = None, value: float = 1.0) -> None:
        key = tuple(sorted((labels or {}).items()))
        with self._lock:
            self._counters[name][key] += value

    def gauge_set(self, name: str, labels: dict[str, str] | None = None, value: float = 0.0) -> None:
        key = tuple(sorted((labels or {}).items()))
        with self._lock:
            self._gauges[name][key] = value

    def histogram_observe(
        self, name: str, labels: dict[str, str] | None = None, value: float = 0.0
    ) -> None:
        key = tuple(sorted((labels or {}).items()))
        buckets = self._histogram_buckets.get(name, DEFAULT_LATENCY_BUCKETS)
        with self._lock:
            if key not in self._histograms[name]:
                self._histograms[name][key] = {
                    "buckets": {b: 0 for b in buckets},
                    "sum": 0.0,
                    "count": 0,
                }
            entry = self._histograms[name][key]
            entry["sum"] += value
            entry["count"] += 1
            for b in buckets:
                if value <= b:
                    entry["buckets"][b] += 1

    def generate_prometheus_text(self) -> str:
        lines: list[str] = []
        with self._lock:
            # Counters
            for name, help_text in self._counter_help.items():
                lines.append(f"# HELP {name} {help_text}")
                lines.append(f"# TYPE {name} counter")
                entries = self._counters.get(name, {})
                if not entries:
                    lines.append(f"{name} 0")
                for key, val in sorted(entries.items()):
                    lbl_str = _format_labels(dict(key))
                    lines.append(f"{name}{lbl_str} {val}")

            # Gauges
            for name, help_text in self._gauge_help.items():
                lines.append(f"# HELP {name} {help_text}")
                lines.append(f"# TYPE {name} gauge")
                entries = self._gauges.get(name, {})
                if not entries:
                    lines.append(f"{name} 0")
                for key, val in sorted(entries.items()):
                    lbl_str = _format_labels(dict(key))
                    lines.append(f"{name}{lbl_str} {val}")

            # Histograms
            for name, help_text in self._histogram_help.items():
                lines.append(f"# HELP {name} {help_text}")
                lines.append(f"# TYPE {name} histogram")
                entries = self._histograms.get(name, {})
                for key, data in sorted(entries.items()):
                    base_labels = dict(key)
                    cum_count = 0
                    for b in self._histogram_buckets.get(name, DEFAULT_LATENCY_BUCKETS):
                        cum_count = data["buckets"][b]
                        b_str = f"{b:g}" if b != float("inf") else "+Inf"
                        bucket_labels = dict(base_labels)
                        bucket_labels["le"] = b_str
                        lines.append(f"{name}_bucket{_format_labels(bucket_labels)} {cum_count}")
                    # +Inf bucket
                    inf_labels = dict(base_labels)
                    inf_labels["le"] = "+Inf"
                    lines.append(f"{name}_bucket{_format_labels(inf_labels)} {data['count']}")
                    lines.append(f"{name}_sum{_format_labels(base_labels)} {data['sum']:.6f}")
                    lines.append(f"{name}_count{_format_labels(base_labels)} {data['count']}")

        return "\n".join(lines) + "\n"


# Global Platform Registry
metrics = MetricsRegistry()

# Initialize standard platform metrics
metrics.register_counter(
    "netci_http_requests_total", "Total count of HTTP requests processed by netCI"
)
metrics.register_histogram(
    "netci_http_request_duration_seconds",
    "HTTP request latency histogram across endpoints in seconds",
    DEFAULT_LATENCY_BUCKETS,
)
metrics.register_histogram(
    "netci_pipeline_duration_seconds",
    "Pipeline execution duration in seconds",
    (10.0, 30.0, 60.0, 120.0, 300.0, 600.0, 1800.0),
)
metrics.register_gauge(
    "netci_active_deployment_leases", "Current number of active target deployment leases"
)
metrics.register_counter(
    "netci_notifications_total", "Total notifications processed by outbox worker by status and event"
)
metrics.register_gauge(
    "netci_notification_outbox_queue_depth", "Depth of the notifications outbox by status"
)
metrics.register_gauge(
    "netci_database_pool_connections", "Number of database connections by state (active, idle, total)"
)
# Readiness as numbers, so an alert can fire on what /readyz says instead of on a probe
# that only tells a load balancer. 1 = ready. `netci_ready` is the overall verdict.
metrics.register_gauge("netci_ready", "1 when this replica would accept and execute work (/readyz), else 0")
metrics.register_gauge(
    "netci_dependency_ready", "1 when the named dependency (database, ci, cd, dcim, cosign, traffic) is ready"
)
metrics.register_gauge("netci_cd_pollers", "Temporal workers polling the delivery task queue, as the server reports them")
metrics.register_gauge("netci_ci_controllers_healthy", "Jenkins controllers the router considers healthy")
metrics.register_gauge("netci_agents", "Edge agents by state (connected, stale) across all replicas")
metrics.register_counter(
    "netci_reconciler_corrections_total", "Runs and deployments the reconciler had to correct because a callback was lost"
)
metrics.register_gauge("netci_replica_info", "Constant 1, labelled with the replica id")


class PrometheusMetricsMiddleware(BaseHTTPMiddleware):
    """ASGI Middleware collecting request rate, status code, and latency."""

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        await super().__call__(scope, receive, send)

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        # Don't trace Prometheus scraping itself to avoid feedback loops
        if request.url.path == "/metrics":
            return await call_next(request)

        start = time.perf_counter()
        route_path = request.url.path

        # Normalize high-cardinality paths (e.g. UUIDs, IDs)
        # Using route template if matched by Starlette router
        match_route = None
        try:
            for route in getattr(request.app, "routes", []):
                match, _ = route.matches(request.scope)
                if getattr(match, "name", "") == "FULL":
                    match_route = getattr(route, "path", None)
                    break
        except Exception:
            match_route = None
        if match_route:
            route_path = match_route

        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            return response
        finally:
            duration = time.perf_counter() - start
            labels = {
                "method": request.method,
                "route": route_path,
                "status_code": str(status_code),
            }
            metrics.counter_inc("netci_http_requests_total", labels)
            metrics.histogram_observe(
                "netci_http_request_duration_seconds",
                {"method": request.method, "route": route_path},
                duration,
            )
