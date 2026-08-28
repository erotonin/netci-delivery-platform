"""Who is on the other end, when there may be a proxy in between.

Two controls depend on this and fail in opposite directions if it is naive. The
`NETCI_AUTH_MODE=none` guard serves loopback only; behind a reverse proxy on the same host
every request arrives from 127.0.0.1, so a check on the socket address alone sees the whole
network as loopback and the guarantee is void. The rate limiter keys unauthenticated
callers by address; behind that same proxy everyone shares one bucket, so one noisy client
throttles the rest.

`X-Forwarded-For` is client-supplied, so trusting it blindly lets a caller choose its own
rate-limit bucket or claim to be local. These tests pin down the middle position: the
header is read only when an operator has said how many proxies are in front, and its mere
presence otherwise marks the address as untrustworthy — which fails closed.
"""

from __future__ import annotations

import pytest

from app.client_address import ClientAddress, resolve_client, trusted_proxy_hops


def test_a_direct_local_caller_is_trusted_loopback():
    client = resolve_client({}, "127.0.0.1", hops=0)
    assert client == ClientAddress(address="127.0.0.1", forwarded=False, trusted=True)
    assert client.is_trusted_loopback


def test_a_direct_remote_caller_is_trusted_but_not_loopback():
    client = resolve_client({}, "203.0.113.9", hops=0)
    assert client.trusted and not client.forwarded
    assert not client.is_trusted_loopback


def test_an_unexpected_proxy_makes_the_address_untrusted():
    """The hole: a reverse proxy on the same host makes every request look local."""

    client = resolve_client({"X-Forwarded-For": "203.0.113.9"}, "127.0.0.1", hops=0)

    assert client.forwarded
    assert not client.trusted
    # This is the assertion that matters: the socket says loopback, and we refuse to
    # believe it, because we cannot tell who is really calling.
    assert not client.is_trusted_loopback


@pytest.mark.parametrize("header", ["X-Forwarded-For", "Forwarded", "X-Real-IP", "x-forwarded-for"])
def test_any_forwarding_header_is_enough_to_stop_trusting_the_peer(header):
    client = resolve_client({header: "anything"}, "127.0.0.1", hops=0)
    assert not client.is_trusted_loopback


def test_a_spoofed_header_cannot_buy_a_caller_loopback_access():
    """A client talking to netCI directly can send whatever it likes."""

    client = resolve_client({"X-Forwarded-For": "127.0.0.1"}, "203.0.113.9", hops=0)
    assert not client.is_trusted_loopback


# --------------------------------------------------- with a configured proxy chain


def test_one_proxy_yields_the_real_client():
    client = resolve_client({"X-Forwarded-For": "203.0.113.9"}, "127.0.0.1", hops=1)
    assert client == ClientAddress(address="203.0.113.9", forwarded=True, trusted=True)


def test_earlier_entries_are_client_supplied_and_must_not_win():
    """With one proxy, only the last entry was written by something we trust. A client
    that prepends addresses is choosing its own rate-limit bucket if we read from the
    left."""

    headers = {"X-Forwarded-For": "10.0.0.1, 198.51.100.7, 203.0.113.9"}
    assert resolve_client(headers, "127.0.0.1", hops=1).address == "203.0.113.9"
    # Two proxies: skip both, and the caller is the entry before them.
    assert resolve_client(headers, "127.0.0.1", hops=2).address == "198.51.100.7"


def test_a_shorter_chain_than_configured_is_not_trusted():
    """Fewer entries than proxies means the header did not come from the chain we were
    told about, so it says nothing about the caller."""

    client = resolve_client({"X-Forwarded-For": "203.0.113.9"}, "127.0.0.1", hops=3)
    assert not client.trusted
    assert not client.is_trusted_loopback


def test_a_missing_header_behind_a_configured_proxy_is_not_trusted():
    client = resolve_client({}, "127.0.0.1", hops=1)
    assert not client.trusted


def test_a_trusted_proxy_can_still_report_a_local_caller():
    """Someone on the box, reaching netCI through the proxy, is genuinely local."""

    client = resolve_client({"X-Forwarded-For": "127.0.0.1"}, "127.0.0.1", hops=1)
    assert client.is_trusted_loopback


# ------------------------------------------------------------------ configuration


def test_hops_default_to_none_configured(monkeypatch):
    monkeypatch.delenv("NETCI_TRUSTED_PROXY_HOPS", raising=False)
    assert trusted_proxy_hops() == 0


@pytest.mark.parametrize("value", ["not-a-number", "-1"])
def test_a_bad_hop_count_is_refused_rather_than_silently_treated_as_zero(monkeypatch, value):
    """Silently falling back to zero would turn a typo into "trust nothing", which reads
    as working until someone checks why the rate limit buckets everyone together."""

    monkeypatch.setenv("NETCI_TRUSTED_PROXY_HOPS", value)
    with pytest.raises(ValueError):
        trusted_proxy_hops()
