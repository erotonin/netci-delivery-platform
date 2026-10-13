# ADR-077: A sign-in that survives its replica

- Status: accepted; measured in the lab (ADR-076 phase 3, T7 and T8: a token minted on r0 accepted by r1 in
  0.05 s; a person signed in on r0 still signed in on r1 after r0's pod was killed, 59 of 60 polls)
- Date: 2026-10-13
- Refines: ADR-076 (replicated homes)

## Context

ADR-076 phase 2 measured the UI through the balancer staying up when a machine dies, but the
person signed in on the dead replica was **anonymous** on the other one: Jenkins keeps a session in
one JVM's memory. The balancer's sticky cookie only names a replica; it cannot carry a session that
replica no longer has.

Two more things were missing for the replicas to look like one Jenkins to their users:
- **Users were not replicated.** An API token minted on r0 was refused by r1, and a script going
  through the VIP reaches either at random. A password changed on one was unknown to the other.
- Each replica had its own **user seed** (Jenkins' per-user secret behind "Invalidate sessions"),
  so nothing could be bound to it on both.

## Decision

1. **Users are replicated through netci-homesync** like items, under `@users/<id>`: a user's
   `config.xml` (password hash, API token hashes, seed, properties) is published on save and
   applied on the other replicas. Posts to a user's pages take that user's lock, so two replicas
   minting tokens for one user at once cannot drop one of them.
2. **`NETCI_SESSION`, a cookie every replica can verify.** A signed-in browser session (one with an
   HTTP session -- API-token calls have none and get no cookie) is given a cookie holding the user's
   id, issue and expiry times, and the SHA-256 of the user's seed, signed with HMAC-SHA256 under a key
   every replica is given (`NETCI_SESSION_KEY_FILE`, at least 32 bytes, from a Secret).
   - A replica with no session for the request but a valid cookie signs the user in again, with
     **the authorities its security realm gives that user now** (`User.impersonate2()`): the cookie
     names a user and carries no authority.
   - Refused: a bad signature, another key, expired, issued in the future, a user that no longer
     exists, a seed that changed (Jenkins' "Invalidate sessions", replicated with the user). A
     cookie that does not verify is cleared rather than re-checked on every request.
   - **Idle lifetime 1 hour by default** (`NETCI_SESSION_TTL_SECONDS`), renewed once half of it has
     passed while the person is active: it ends like a session, not like a days-long "remember me".
   - Sign-out clears it on the `/logout` request itself, before Jenkins handles it, and never
     re-issues it there. (The first version cleared it from Jenkins' logout listener; the unit test
     showed the next page signed the person straight back in.)
   - `HttpOnly`, `SameSite=Lax`, `Secure` under HTTPS. Off unless the key is configured.

## What it costs, said plainly

- A **POST** sent with a page loaded from the dead replica carries a CSRF crumb bound to the old
  session and is refused once (403); reloading the page fixes it. GETs are transparent.
- The key is a shared secret: whoever holds it can sign a session for any user. It lives in a
  Kubernetes Secret mounted only into the controllers, as `secrets/` already does.
- A realm that cannot look a user up by name (some SSO set-ups) cannot restore from the cookie;
  the person is sent to sign in again, as before.

## Rejected

- **Replicating the servlet session** (Jetty's session data stores): Jenkins' sessions hold
  objects that are not designed to be serialised across JVMs, and it would put every session write
  on the network path of every request.
- **Forcing Jenkins' own "remember me"**: lasts days and is off unless the person ticks the box;
  the property needed is "survives a failover", not "survives closing the browser".
- **Sticky sessions alone**: they pin a person to a replica, which is exactly what fails here.
