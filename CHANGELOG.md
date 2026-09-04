# Changelog

All notable changes to netCI will be documented here. The format follows Keep a
Changelog and releases use Semantic Versioning once the project reaches 1.0.0.

## [Unreleased]

### Added

- Open-source governance, contribution and security policies.
- Production Portal container and real DCIM, Jenkins, Temporal, PostgreSQL,
  Ansible and Cosign integration seams.
- Durable security evidence, production-request completion and idempotency.

### Changed

- **Deployment leases, monotonic fencing tokens and safe log sequences.**
  Mutually exclusive deployments per (application, environment, target) are enforced
  directly in PostgreSQL via partial unique index. Fencing tokens prevent stale/superseded
  workflows from overwriting newer deployment state. Pipeline log sequence allocation
  uses an atomic per-run counter table to prevent concurrent primary key clashes.
  Deployments gain explicit lifecycle states (rollback_in_progress, rolled_back, rollback_failed).
  See ADR-016.
- **Machine callbacks use scoped, short-lived workload tokens.** The shared
  `NETCI_PIPELINE_API_KEY` could not say which build was calling; a token now names
  one workload, one application and one run or deployment, plus its scopes. A token
  for run A cannot write to run B, a Jenkins token cannot report a deployment result,
  and a terminal-scope token is single-use. Outside local mode netCI refuses to start
  without `NETCI_WORKLOAD_TOKEN_KEYS`, and the shared key is refused unless
  `NETCI_ALLOW_LEGACY_PIPELINE_KEY` declares a migration window. See ADR-015.
- **Pipeline `parameters` is a closed build-input allowlist.** Deployment targets,
  namespaces, credential references, artifact URLs, playbooks, health commands and
  rollback behaviour can no longer be supplied by a caller: naming one is now
  `422 DEPLOYMENT_PARAMETER_NOT_ACCEPTED` rather than being silently overridden.
  `commitSha` must be hexadecimal and `branch` a git refname.
- `POST /applications/{id}/pipeline-runs` is platform-admin/machine only; developers
  use `POST /modules/{id}/pipeline-runs`, which binds the run to the module's
  registered deployment target.
- Module responses now include `ownerTeam`.
- **PostgreSQL is canonical at request time.** `DeliveryPlatform` and `PortalService`
  no longer load state into process memory at start-up or answer requests from it;
  every command reads and writes inside one transaction. Multiple API replicas now
  share state without restarts, and a replica that loses a race recovers on its next
  request instead of staying stale. See ADR-014.
- **Module onboarding is atomic.** `POST /systems/{systemId}/modules` writes the
  delivery application, the Portal module, the audit record and the idempotency
  record in one transaction. A retry carrying the same `Idempotency-Key` returns the
  original module (`201`) rather than `409 MODULE_EXISTS`, and a failure part-way
  through no longer leaves an application that no module points at.
- `PortalReadModel` is renamed `PortalService`; it issues commands, and the old name
  said otherwise.
- An unreachable database now returns `503 PERSISTENCE_UNAVAILABLE` on read paths too,
  rather than serving a cached snapshot. A duplicate-key race returns
  `409 CONCURRENT_MODIFICATION` instead of a `503`.
- Runtime installations start empty unless demo data is explicitly enabled.
- Production promotion reuses the source artifact and rebinds the server-owned
  production target.
- Approval/request identities now come only from the authenticated principal;
  undeclared identity fields are rejected instead of silently ignored.
- Ansible runtime parameters can no longer override the verified artifact,
  deployment, application or environment identity.
- Non-local runtimes now fail at startup when auth, CI, CD, security evidence or
  deploy-time signature verification is disabled.
- Portal projections display unknown state instead of invented healthy data.

### Removed

- Runtime frontend fixtures and editable deployment scripts.
