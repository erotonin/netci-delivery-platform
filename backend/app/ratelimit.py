"""A ceiling on how fast one caller can hit the API.

Not a defence against a determined attacker -- that belongs at the edge, in a gateway that
sees traffic before netCI does. This is the other thing rate limiting is for: stopping one
misconfigured client from taking the platform down for everyone. A CI job in a retry loop,
a dashboard polling every 100ms, a script with a `while true` -- none of them are hostile,
and all of them are indistinguishable from an outage if the API just keeps answering until
it falls over.

The window is fixed rather than sliding, and the counters live in this process. Both are
deliberate: a sliding window needs per-request timestamps, and shared counters need Redis,
and neither buys anything against the failure mode above. Behind several replicas the
effective limit is the configured one times the replica count, which is documented rather
than papered over.

`NETCI_RATE_LIMIT` is requests per window per caller, `NETCI_RATE_LIMIT_WINDOW` the window
in seconds. Zero disables it, which is the default: a local reference implementation that
throttles a developer's own test loop is a worse default than no limit at all.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class Decision:
    allowed: bool
    limit: int
    remaining: int
    retry_after: int
    reset_at: int


class FixedWindowRateLimiter:
    """Count requests per caller per window, in memory.

    Callers are identified by whatever key the caller of `check()` supplies -- an
    authenticated subject where there is one, the client address otherwise. Keying on the
    subject matters: a shared NAT would otherwise make one team's traffic look like one
    caller, and throttle a whole office because one person looped.
    """

    def __init__(self, limit: int, window_seconds: float) -> None:
        self.limit = limit
        self.window_seconds = window_seconds
        self._lock = threading.Lock()
        self._counts: dict[str, tuple[int, float]] = {}
        self._last_swept = 0.0

    @property
    def enabled(self) -> bool:
        return self.limit > 0 and self.window_seconds > 0

    def _sweep(self, now: float) -> None:
        """Drop windows that have expired, so the map does not grow without bound.

        Called at most once per window: an API with many distinct callers would otherwise
        accumulate an entry per caller forever, which is a slow leak rather than a limit.
        """

        if now - self._last_swept < self.window_seconds:
            return
        self._counts = {key: value for key, value in self._counts.items() if value[1] > now}
        self._last_swept = now

    def check(self, key: str, *, now: float | None = None) -> Decision:
        moment = now if now is not None else time.monotonic()
        if not self.enabled:
            return Decision(True, self.limit, self.limit, 0, 0)

        with self._lock:
            self._sweep(moment)
            count, expires_at = self._counts.get(key, (0, 0.0))
            if moment >= expires_at:
                count, expires_at = 0, moment + self.window_seconds
            count += 1
            self._counts[key] = (count, expires_at)

        remaining = max(0, self.limit - count)
        retry_after = max(1, int(expires_at - moment + 0.999))
        return Decision(
            allowed=count <= self.limit,
            limit=self.limit,
            remaining=remaining,
            retry_after=retry_after,
            reset_at=retry_after,
        )

    def reset(self) -> None:
        with self._lock:
            self._counts.clear()
            self._last_swept = 0.0


def build_rate_limiter() -> FixedWindowRateLimiter:
    """Read the limit from configuration. Zero, the default, disables it."""

    try:
        limit = int(os.getenv("NETCI_RATE_LIMIT", "0"))
        window = float(os.getenv("NETCI_RATE_LIMIT_WINDOW", "60"))
    except ValueError as exc:
        raise ValueError(
            "NETCI_RATE_LIMIT must be an integer and NETCI_RATE_LIMIT_WINDOW a number of seconds"
        ) from exc
    if limit < 0 or window <= 0:
        raise ValueError("NETCI_RATE_LIMIT cannot be negative and NETCI_RATE_LIMIT_WINDOW must be positive")
    return FixedWindowRateLimiter(limit, window)
