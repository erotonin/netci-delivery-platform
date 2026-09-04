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
