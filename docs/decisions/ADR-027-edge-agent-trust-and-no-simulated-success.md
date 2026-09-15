# ADR-027: Edge-agent trust model, and the removal of every simulated success path

Status: Accepted.

## Context

An audit on 2026-09-15 (`docs/KIEM-TOAN-2026-09-15.md`) found that the most recent
"enterprise improvements" commit had reintroduced, in four places, the thing this platform
exists to prevent: a green status the platform had not established.

1. **A simulated CI worker started by default.** `local_runner.py` picked up queued runs,
   invented an artifact digest (`sha256(run_id + commit)`), wrote a hard-coded CI report
   (`42 tests, 94.5% coverage, scan passed`), logged "VALID signature" where none existed,
   and published that as security evidence -- which the policy gate then accepted, producing
   deployments of artifacts that did not exist. It ran whenever `NETCI_CI_MODE != jenkins`.
   Production was protected only because `ci_launcher.py` refuses `CI_MODE=none` outside
   local mode; the runner itself had no guard.
2. **"Fast-Apply CD" created deployments marked `healthy` directly**, with no lease, no
   workflow, no health check, and -- when no artifact existed -- a digest hashed from the
   module's name. It also emitted a DORA deployment event and reported `leadTimeSeconds: 2`.
3. **`/git/info` answered with a hard-coded commit SHA** because `subprocess` was never
   imported and the `NameError` was swallowed by `except Exception: pass`. The same SHA was
   the frontend's fallback for which commit to build. The endpoint also carried a
   hard-coded catalog of sample apps pointing at GitHub repositories that do not exist.
4. **The edge-agent WebSocket accepted anyone.** `accept()` ran before any check, the
   hostname came from the query string, and `execute` matched hostnames by substring -- so
   an agent named `a` received the commands meant for every host containing an `a`. The
   endpoint, and twelve others, were hidden from OpenAPI with `include_in_schema=False`,
   which is also what hid them from the contract test.

## Decision

**Nothing simulated remains in the runtime.** `local_runner.py` is deleted, not gated.
Local development runs the Jenkins lab; a developer who wants to see a run in the Portal
runs a real one. The demo seed keeps only its three catalog entries and no longer
fabricates pipeline history.

**Applying configuration is a deployment.** `apply_config_revision` now calls
`DeliveryPlatform.redeploy_artifact`, which requires a real digest from a succeeded run,
re-evaluates the artifact policy, takes the lease, sets `pending_approval` (production) or
`deploying`, and starts the real workflow. Health is whatever the worker reports. With no
artifact it refuses with `409 NO_DEPLOYABLE_ARTIFACT`.

**Build identity is established or `null`.** `/git/info` reads `NETCI_BUILD_COMMIT` or
`git rev-parse`, and reports the source; the UI disables the trigger button rather than
guessing. Sample applications are discovered from `sample-apps/` at request time, and
their repository URL comes from `NETCI_SAMPLE_APPS_REPOSITORY_BASE` or is `null`.

**Agents prove which host they are.** An agent token (`Workload.AGENT`, scope
`agent:connect`) is minted by a platform admin for one hostname via
`POST /api/v1/agents/token`; the daemon presents it as a bearer header; the hostname is
the token's claim and the query string cannot choose it. `execute` matches exactly and is
platform-admin only. The agent registry is process-local, which is a documented limitation
(`/api/v1/agents/status` names the replica), not a hidden one; a database-backed registry
is the next step if multi-replica agent dispatch is needed.

**Every route is in the contract.** `include_in_schema=False` is used only for
`/metrics`. `POST /api/v1/workspaces/upload` is removed outright: it wrote to a hard-coded
absolute path with no size limit, and nothing could build from what it stored, because
`repositoryUrl` is validated as `HttpUrl` and rejects `local://`.

**The server names the actor.** `ServerMaintenanceRequest.updatedBy` is gone; the actor is
the principal, and maintenance mode is platform-admin only because it decides where
deployments may go.

## Consequences

The dashboard on a fresh local install is empty until a real pipeline runs. That is the
correct state, and it is the reason the Jenkins lab exists.

`test_state_machine_enforcement.py`, `test_agent_channel_security.py` and updated
enterprise tests pin each of the four regressions. The CI workflow should run
`pytest backend/tests tests/contract` in full; it does not today, which is how a red
contract test reached `main`.
