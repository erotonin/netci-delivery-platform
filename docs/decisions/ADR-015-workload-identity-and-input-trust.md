# ADR-015: Workload identity for machine callbacks, and a closed build-input boundary

Status: Accepted.

## Context

Two trust boundaries were open.

**One shared secret for every machine.** `NETCI_PIPELINE_API_KEY` authenticated every
callback. It proved the caller held netCI's pipeline secret and nothing more: not which
build was calling, not which application it belonged to, and not whether the caller was a
CI controller reporting a build or a worker reporting a deployment. Any holder could
report any result for any run, and a compromised Jenkins agent could mark any deployment
healthy. A secret that answers "are you the pipeline?" is a password shared by every
workload in the estate, not an identity.

**An open parameter map.** `PipelineRunCreate.parameters` was `dict[str, object]`, passed
through the domain into the CD orchestrator, where the runtime adapters read `target_hosts`,
`namespace`, `kubeconfig_ref`, `artifact_url` and task lists out of it. A browser could
choose which machines a deployment touched and which credential it used by adding a JSON
key. `PortalService.delivery_parameters` overrode some of these afterwards, which meant an
attempt was silently neutralised — indistinguishable, to the caller, from having worked.

## Decision

### Short-lived signed callback tokens

We considered workload OIDC (SPIFFE/Kubernetes projected tokens) and mTLS first, since
both are stronger and neither requires netCI to hold a signing key. Both were rejected
*for this phase*: netCI's reference deployment runs Jenkins agents and Temporal workers as
plain processes on hosts and in Compose, where there is no workload attestor to trust and
no per-workload certificate authority. Introducing one is an infrastructure decision an
adopter has to make, and the seam here does not prevent it — `_workload_principal` is a
single function, and an OIDC verifier can replace the HMAC one without touching an
endpoint. That is the migration path, and it is recorded here so the shortcut is visible.

netCI mints and verifies its own compact tokens (`app/workload_identity.py`). Every token
names `iss`, `aud`, `sub` (the workload kind), `application_id`, exactly one of
`pipeline_run_id`/`deployment_id`, its `scopes`, `iat`/`exp` and a `jti`.

Deliberately not a general JWT library: netCI is both issuer and verifier, so the
algorithm is fixed at the verifier — there is no `alg` to confuse — the claim set is
closed, and there is nothing to negotiate. Keys carry a `kid` and several may be
configured at once, which makes rotation a config change rather than an outage: publish
the new key, let both verify, retire the old one.

Authentication proves the token is real; a separate check proves it is for *this*
resource. `_authorize_callback` compares the route's id against the claim, the operation's
scope against the token's scopes, and the endpoint's expected workload against `sub`.
A `WORKLOAD_SCOPES` table means a Jenkins token cannot even be *minted* with
`deployment:result`, and a scope a workload may never hold is dropped on verification too,
so a mis-issued token cannot become an escalation.

Terminal scopes are single-use. `deployment:result` ends a piece of work, so its `jti` is
claimed by primary key in `callback_token_uses`; a replayed token cannot overwrite a newer
result with an older one, and because the claim is an `INSERT ... ON CONFLICT DO NOTHING`,
two replicas handed the same replayed token cannot both decide it was unused.

The shared key survives only in local mode or behind an explicit
`NETCI_ALLOW_LEGACY_PIPELINE_KEY` migration window. Outside those it is refused even when
set — otherwise "we rolled out workload identity" and "the old key still works" are both
true at once, and only the second one matters to whoever has the key. A non-local runtime
with neither configured refuses to start.

### A closed build-input boundary

`app/build_inputs.py` states the boundary as data. Three kinds of value were confused in
one map:

* **Build inputs** are the caller's business — a build profile, a flag, a log level.
  Allow-listed by name, scalars or one level of scalar object, bounded in count, string
  length, nesting depth and total bytes.
* **Application configuration** is immutable at run time.
* **Deployment parameters** are server-managed and computed from the module's registered
  configuration, applied *after* caller input so a caller cannot shadow one by guessing.

A deployment-controlled key is **refused with 422, not dropped**. Silently dropping teaches
a caller that its override worked; the operator finds out at the incident review. The
comparison is case- and separator-insensitive, so `TARGET-HOSTS` does not get past it.

`commitSha` is constrained to hexadecimal — anything else is not a commit — and `branch`
to git-refname characters plus an explicit rejection of `..`, leading `/` or `-`, and
trailing `/`. Both values reach a `git checkout` in a CI template; a character class alone
still lets `../../etc/passwd` through, which is why the rule is written twice.

`POST /applications/{id}/pipeline-runs` is restricted to platform administrators and
machine identities. It names an application rather than a module, so it skips the Portal
lookup that binds a run to its module's registered target — a route that cannot apply the
target policy should not be the one developers use. `/modules/{id}/pipeline-runs` is the
developer entry point and resolves the server-owned target for them.

## Consequences

Adopters must configure `NETCI_WORKLOAD_TOKEN_KEYS` (or `..._FILE`) before running outside
local mode. CI templates and workers must obtain a scoped token rather than reading a
shared key from the environment; the launcher mints one per run, and
`POST /pipeline-runs/{id}/callback-token` re-issues one whose lifetime expired.

Callers sending deployment keys in `parameters` now get a `422` where they previously got a
`202`. That is the intended break, and the error names the key so the caller can act on it.

`ownerTeam` is now part of the module response, so the Portal can show ownership without a
second request.

Not yet done, and owned by later phases: `callback_token_uses` has no retention job, so
expired rows accumulate until Phase 9's retention work; and there is no revocation list —
a token is bounded by its expiry, not cancellable before it.

## Verification

`backend/tests/test_workload_identity.py` covers minting, verification, rotation across
two keys, expiry, wrong issuer, wrong audience, wrong signing key, a tampered claim,
`alg: none`, refusal of a short key, and the scope table in both directions.
`backend/tests/test_callback_authorization.py` drives the real API: a token for run A is
refused on run B, a Jenkins token cannot report a deployment result, a terminal token
cannot be replayed, an expired or wrong-audience token is refused, and neither the token
nor the signing key appears in any response, log line or audit record.
`backend/tests/test_build_inputs.py` covers every deployment-controlled key, nested keys,
depth, size and type limits, shell- and traversal-shaped branches and commits, and that
the registered target still wins.
