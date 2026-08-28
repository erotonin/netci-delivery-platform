"""Who is on the other end of a request, when there may be a proxy in between.

Two things in netCI depend on this and get it wrong in opposite directions if it is naive:

  * The `NETCI_AUTH_MODE=none` guard serves loopback only, so that forgetting to configure
    authentication cannot publish production approval to the network. Behind a reverse
    proxy on the same host -- the ordinary way to terminate TLS -- every request arrives
    from 127.0.0.1, so a naive check sees the whole internet as loopback and the guarantee
    is void.
  * The rate limiter keys unauthenticated callers by address. Behind that same proxy every
    caller shares one bucket, so one noisy client throttles everyone: the protection
    becomes the outage it was meant to prevent.

`X-Forwarded-For` cannot simply be trusted, because a client talking to netCI directly can
send whatever it likes and would then choose its own rate-limit bucket, or claim to be
loopback. So the header is used only when an operator has said how many proxies sit in
front (`NETCI_TRUSTED_PROXY_HOPS`), and until then its mere presence marks the request as
*not* trustworthy as loopback -- which fails closed rather than open.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

# Anything that means "a proxy handled this". Presence is what matters when we have not
# been told how to interpret them; the values are only read when hops are configured.
FORWARDING_HEADERS = ("x-forwarded-for", "forwarded", "x-real-ip")

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost", "testclient"})


@dataclass(frozen=True)
class ClientAddress:
    address: str
    """The best available identity for the caller: the real client when a trusted proxy
    chain is configured, otherwise whatever connected to us."""

    forwarded: bool
    """A proxy is in the path, whether or not we know how to read its headers."""

    trusted: bool
    """Whether `address` can be relied on for a security decision. False when a proxy is
    in the path and we were not told how many hops to skip."""

    @property
    def is_trusted_loopback(self) -> bool:
        """Safe to treat as "someone at the console", which is what `none` mode allows."""

        return self.trusted and self.address in LOOPBACK_HOSTS


def trusted_proxy_hops() -> int:
    raw = os.getenv("NETCI_TRUSTED_PROXY_HOPS", "0").strip()
    try:
        hops = int(raw or "0")
    except ValueError as exc:
        raise ValueError("NETCI_TRUSTED_PROXY_HOPS must be an integer number of proxies") from exc
    if hops < 0:
        raise ValueError("NETCI_TRUSTED_PROXY_HOPS cannot be negative")
    return hops


def resolve_client(headers, peer: str | None, *, hops: int | None = None) -> ClientAddress:
    """Work out who is calling, given the socket peer and the request headers.

    `hops` is how many proxies netCI sits behind. With one proxy, the client address is
    the last entry in `X-Forwarded-For`; earlier entries were supplied by the client and
    are forgeable. Counting from the right is the only correct way to read that header.
    """

    peer_address = peer or ""
    count = trusted_proxy_hops() if hops is None else hops
    lowered = {name.lower(): value for name, value in headers.items()}
    forwarded = any(name in lowered for name in FORWARDING_HEADERS)

    if count <= 0:
        # Not configured for a proxy. If one is clearly in the path we cannot say who the
        # client is, so the address is untrusted -- and a security check that depends on
        # it has to refuse rather than guess.
        return ClientAddress(address=peer_address, forwarded=forwarded, trusted=not forwarded)

    chain = [item.strip() for item in lowered.get("x-forwarded-for", "").split(",") if item.strip()]
    if len(chain) < count:
        # Fewer entries than proxies means the header did not come from the chain we were
        # told about. Fall back to the peer, and do not call it trusted.
        return ClientAddress(address=peer_address, forwarded=forwarded, trusted=False)
    return ClientAddress(address=chain[-count], forwarded=True, trusted=True)
