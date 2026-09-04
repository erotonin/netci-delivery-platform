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
