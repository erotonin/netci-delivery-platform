"""The ceiling on how fast one caller can hit the API.

This is not a defence against a determined attacker; that belongs in a gateway. It is the
other thing rate limiting is for: one misconfigured client -- a CI job in a retry loop, a
dashboard polling every 100ms -- is indistinguishable from an outage if the API keeps
answering until it falls over.
"""

from __future__ import annotations

import importlib

import pytest
from fastapi.testclient import TestClient

from app.ratelimit import FixedWindowRateLimiter, build_rate_limiter


# ------------------------------------------------------------------ the limiter itself


def test_it_is_off_by_default():
    """A local reference implementation that throttles a developer's own test loop is a
    worse default than no limit at all."""

    assert not FixedWindowRateLimiter(0, 60).enabled
    assert FixedWindowRateLimiter(5, 60).enabled


def test_requests_are_allowed_up_to_the_limit_and_then_refused():
    limiter = FixedWindowRateLimiter(3, 60)
    verdicts = [limiter.check("dana", now=0.0) for _ in range(5)]

    assert [item.allowed for item in verdicts] == [True, True, True, False, False]
    assert [item.remaining for item in verdicts] == [2, 1, 0, 0, 0]
    assert verdicts[-1].retry_after == 60


def test_the_window_resets():
    limiter = FixedWindowRateLimiter(2, 10)
    assert limiter.check("dana", now=0.0).allowed
    assert limiter.check("dana", now=1.0).allowed
    assert not limiter.check("dana", now=2.0).allowed

    # Same caller, next window.
    assert limiter.check("dana", now=11.0).allowed


def test_callers_are_counted_separately():
    """One noisy client must not throttle everyone else."""

    limiter = FixedWindowRateLimiter(1, 60)
    assert limiter.check("dana", now=0.0).allowed
    assert not limiter.check("dana", now=0.0).allowed
    assert limiter.check("sam", now=0.0).allowed


def test_expired_windows_are_swept_so_the_map_does_not_grow_forever():
    """An API with many distinct callers would otherwise leak an entry per caller."""

    limiter = FixedWindowRateLimiter(10, 5)
    for index in range(50):
        limiter.check(f"caller-{index}", now=0.0)
    assert len(limiter._counts) == 50  # noqa: SLF001 - the leak is the thing under test

    # A request in a later window sweeps what expired.
    limiter.check("caller-0", now=100.0)
    assert len(limiter._counts) == 1  # noqa: SLF001


@pytest.mark.parametrize(
    "limit, window", [("not-a-number", "60"), ("10", "nonsense"), ("-1", "60"), ("10", "0")]
)
def test_a_bad_configuration_is_refused_at_startup_not_ignored(monkeypatch, limit, window):
    monkeypatch.setenv("NETCI_RATE_LIMIT", limit)
    monkeypatch.setenv("NETCI_RATE_LIMIT_WINDOW", window)
    with pytest.raises(ValueError):
        build_rate_limiter()


# --------------------------------------------------------------------- through the API


@pytest.fixture
def limited_app(monkeypatch):
    monkeypatch.setenv("NETCI_RATE_LIMIT", "3")
    monkeypatch.setenv("NETCI_RATE_LIMIT_WINDOW", "60")

    import app.main as main

    module = importlib.reload(main)
    yield TestClient(module.app), module

    monkeypatch.delenv("NETCI_RATE_LIMIT", raising=False)
    monkeypatch.delenv("NETCI_RATE_LIMIT_WINDOW", raising=False)
    importlib.reload(main)


def test_the_api_answers_429_with_retry_after(limited_app):
    client, _ = limited_app

    for _ in range(3):
        assert client.get("/applications").status_code == 200

    throttled = client.get("/applications")
    assert throttled.status_code == 429
    assert throttled.json()["code"] == "RATE_LIMITED"
    # A client that cannot tell how long to wait will simply retry immediately.
    assert int(throttled.headers["Retry-After"]) > 0
    assert throttled.headers["X-RateLimit-Limit"] == "3"
    # Still traceable: a throttled request is one an operator may well be asked about.
    assert throttled.headers["X-Correlation-Id"]


def test_healthz_is_never_throttled(limited_app):
    """A load balancer polls this constantly; throttling it turns a rate limit into an
    outage, because the platform then reports itself unhealthy."""

    client, _ = limited_app
    for _ in range(10):
        assert client.get("/healthz").status_code in {200, 503}


def test_different_credentials_are_different_callers(limited_app):
    client, _ = limited_app
    machine = {"Authorization": "Bearer netci-local-pipeline-key"}

    for _ in range(3):
        client.get("/applications", headers=machine)
    assert client.get("/applications", headers=machine).status_code == 429

    # The unauthenticated caller has its own budget and is unaffected.
    assert client.get("/applications").status_code == 200


def test_the_limiter_never_holds_a_credential_in_memory(limited_app):
    """The counter map keys on a hash. Keying on the token itself would make it a list of
    live credentials sitting in process memory for as long as the window lasts."""

    client, module = limited_app
    token = "Bearer a-very-secret-token-value"
    client.get("/applications", headers={"Authorization": token})

    keys = list(module.rate_limiter._counts)  # noqa: SLF001
    assert keys, "the request should have been counted"
    assert all("a-very-secret-token-value" not in key for key in keys)


def test_healthz_reports_the_configured_limit(limited_app):
    client, _ = limited_app
    assert client.get("/healthz").json()["engines"]["rateLimit"] == "3/60s"
