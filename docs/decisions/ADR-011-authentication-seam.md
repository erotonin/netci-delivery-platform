# ADR-011: Authentication is a seam, and the actor comes from the credential

Status: Accepted.

## Context

netCI's whole value proposition is governance: production needs an approval, every
transition names an actor, and the audit trail is evidence rather than narrative. Until
this decision, none of that was true in the way it read.

Three findings, all in the shipped code:

1. Every human-facing endpoint was unauthenticated. `POST /production-requests/{id}/approve`
   and `POST /deployments/{id}/rollback` were reachable by anyone who could reach the port.
   Only the eight CI-callback endpoints checked a shared key.
2. The actor was self-asserted. `ApprovalRequest.actor` defaulted to `"local-reviewer"`,
   and the Portal sent the literal string `'Admin'`. The audit trail recorded whatever the
   caller typed.
3. `require_environment_permission(environment, role)` — the control that reserves
   production for reviewers — was written, documented, and never called from anywhere.

Any one of these makes the other two moot. Together they meant the approval step was a
button, not a control.

## Decision

**Authentication is a configurable seam**, selected at the composition root by
`NETCI_AUTH_MODE`, exactly as `NETCI_CI_MODE` and `NETCI_CD_MODE` select engines:

- `none` — every caller is anonymous with every role, **and netCI serves loopback only**.
- `token` — bearer tokens listed as SHA-256 hashes in a file that is re-read on change.
- `oidc` — bearer JWTs from a corporate identity provider, verified against its JWKS.

The domain does not import the auth module. It still takes an actor string; what changed is
that the composition root now guarantees the string came from a verified identity.

Three rules follow:

- **The actor is the credential holder.** `actor`, `requestedBy` and `createdBy` were
  removed from request bodies rather than left to be ignored — a field the server accepts
  and silently discards is worse than one that does not exist.
- **Machine endpoints are machine-only.** The `pipeline` role has no human escape hatch,
  in either direction: a platform-admin cannot post a CI result, and the pipeline key
  cannot approve a deployment.
- **The requester may not approve their own production release.** `pipeline_runs.started_by`
  (migration `0003`) records who started a run so there is something to compare against.

## Consequences

`none` fails closed rather than open. A deployment that forgets to configure authentication
is not a wide-open netCI on the network; it is one that refuses non-loopback callers with
`AUTH_NOT_CONFIGURED`. `/healthz` reports the active mode so the posture is visible from
outside.

Separation of duties is inactive in `none` mode. Every caller is the same subject there, so
the check would refuse every approval while separating nobody. Runs predating this change
have no `started_by`; an unknown requester is allowed rather than locking out every
deployment created before the upgrade.

The Portal can no longer decide who the user is. It asks `GET /me`, and the server's answer
is both the session and the source of the roles the UI shows. The previous login screen
accepted any username and password and never spoke to the backend at all.

`cryptography` becomes a direct dependency, pinned explicitly rather than relied on as a
transitive dependency of `temporalio`.

Roles are global. Per-application or per-team ACLs are not implemented; an organisation
that needs them extends `requires()` in `backend/app/main.py`. Rate limiting and tenant
isolation remain out of scope.

## Alternatives considered

**Session cookies with a local password store.** Rejected: it makes netCI a place where
employee credentials live, obliges it to handle rotation, lockout and offboarding, and
duplicates what the corporate directory already does correctly.

**Depending on a reverse proxy to authenticate.** Rejected as the only mechanism. It works,
but it makes the API insecure by default whenever it is reached directly — during a
migration, from inside the cluster, or in a developer's port-forward — and it gives netCI
no identity to write into the audit trail.

**A JWT library.** `PyJWT[crypto]` would be conventional. The verifier here is ~80 lines
over `cryptography`, avoids a dependency whose defaults have historically been permissive
(algorithm confusion, optional audience checks), and is exercised by
`backend/tests/test_auth_oidc.py` against a real key pair for each classic forgery. Revisit
if the token formats in use outgrow it.

## Verification

`backend/tests/test_authorization.py` runs the API in `token` mode with four distinct
identities and asserts: an unauthenticated caller is refused; a viewer cannot write; a
developer cannot reach production; the approver recorded is the credential holder and not
the request body; the person who started a production run cannot approve it; a revoked
token stops working without a restart; and the token file never contains a usable
credential.

`backend/tests/test_auth_oidc.py` covers the verifier itself: `alg: none`, RS256→HS256
algorithm confusion, tampered payloads, wrong issuer, wrong audience, expired and
expiry-less tokens, unknown and missing `kid`, key rotation, and groups that map to no role.
