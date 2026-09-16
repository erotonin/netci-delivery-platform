# ADR-034: The browser signs in at the identity provider; the Portal carries no issuer

Status: Accepted.

## Context

netCI verified OIDC tokens (issuer, audience, signature via JWKS, expiry, group → role
map) but the Portal's "OIDC login" was a text box: paste an access token. The acceptance
harness obtained tokens with the password grant, which proves verification and nothing
about a person signing in. No browser ever went to Keycloak and came back.

## Decision

**Authorization Code + PKCE, written out, no library** (`frontend/src/auth/oidc.ts`).
The Portal asks `GET /auth/config` how to sign in; for `oidc` the server answers with
the public client id and the provider's endpoints, read from the issuer's discovery
document (fetched lazily, kept ten minutes). The bundle carries no issuer, client id or
endpoint: the same build serves every installation, and a browser cannot be pointed at
another provider by editing a config file (ADR-015, applied to configuration).

The Portal generates a verifier and a state, keeps them in `sessionStorage` (this tab,
this attempt), sends the browser to the authorization endpoint with the S256 challenge,
and on return checks the state before exchanging the code with the verifier. The
`id_token` then goes through the same path as a pasted token: `GET /me` decides who the
person is and what they may do. A callback whose state this tab did not issue is
refused before any exchange. Paste-a-token stays as the fallback (and the only option
when discovery is unavailable or no browser client is configured).

**A separate public client** (`netci-portal`, PKCE required) beside the confidential
`netci` client the harness uses. Its tokens carry `aud: netci` through an audience
mapper, so netCI's audience check needs no change; the groups mapper supplies the
claim the role map reads.

## What was rejected

- *Keeping paste-a-token as the login.* It is what an operator does in a break-glass,
  not how a person signs in; it also trains people to copy bearer tokens around.
- *An OIDC client library.* Four steps, ~120 lines, and every line of a login flow is
  worth reading; a library would hide the state check, which is the part that matters.
- *Implicit flow / tokens in the fragment.* Deprecated for the reason it would be wrong
  here: the token would land in the address bar and the browser history.
- *The confidential client in the browser.* A secret in a bundle is not a secret.

## Consequences

- `frontend/e2e/oidc-login.spec.ts` drives a real browser to the lab Keycloak: SSO
  button → provider's login form → password typed there → back with code+state →
  exchange → `/me` answers `method: oidc` with roles → name in the shell; a forged
  callback raises "state mismatch" and calls no token endpoint. 2 passed against the
  live lab (`evidence/oidc-browser-login.json`).
- The other Playwright specs assume `NETCI_AUTH_MODE=none` (local development) and are
  not run against the OIDC lab; the OIDC spec skips itself, loudly, when the API is not
  in OIDC mode.
- Logout still only forgets the token in the tab; the provider session survives until it
  expires (`endSessionEndpoint` is published but not yet called).
