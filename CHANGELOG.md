# Changelog

All notable changes to netCI will be documented here. The format follows Keep a
Changelog and releases use Semantic Versioning once the project reaches 1.0.0.

## [Unreleased]

### Added - Phase 9 (P1.4): Observability, Transactional Outbox, Connection Pooling & Disaster Recovery
- **Thread-Safe PostgreSQL Connection Pooling (`backend/app/store/postgres.py`)**:
  - `PostgresConnectionPool` with bounded concurrency (`NETCI_DB_POOL_MIN`, `NETCI_DB_POOL_MAX`), timeout on pool exhaustion (`TimeoutError`), connection liveness validation (`SELECT 1`), and graceful cleanup.
  - Transactions acquire and return connections seamlessly, preventing socket starvation.
- **Structured JSON Logging & Credential Redaction (`backend/app/logging.py`)**:
  - Standardized JSON formatter emitting ISO8601 timestamps, log levels, correlation IDs, and service metadata.
  - Automatic redaction of sensitive credentials (Bearer tokens, passwords, private keys, SCM secrets).
  - ContextVar propagation via `correlation_id_middleware` and HTTP `X-Correlation-ID` header.
- **Prometheus Metrics Exposition (`backend/app/metrics.py`)**:
  - Real-time tracking of HTTP request rates, response latency histograms, connection pool gauges, and notification outbox depth.
  - Standard Prometheus exposition format exposed at `GET /metrics`.
- **Transactional Outbox & Notification Worker (`backend/app/notifications.py`, migration `0015`)**:
  - Added `notifications` table and `UnitOfWork.notifications` queue for atomic transactional notification emission.
  - Background `NotificationOutboxWorker` with exponential backoff (2s, 4s, 8s, 16s, 32s) and dead-letter queue routing (`status='dead_letter'`).
  - Notification management APIs: `GET /notifications` and `POST /notifications/{id}/retry`.
- **Cursor-Based Monotonic Pagination**:
  - High-performance cursor pagination for `/notifications`, `/pipeline-runs`, `/deployments`, and `/audit-events`.
  - Added composite indexes (`(created_at, id)`) in migration `0015`.
- **Retention Management & Purge Policies (`backend/app/retention.py`, `scripts/netci_retention_purge.py`)**:
  - Automated purging of expired callback tokens, delivered outbox notifications, and stale webhook events.
  - Operational endpoint `POST /admin/retention/purge` and standalone CLI tool.
- **AES-256-GCM Encrypted Backups & Disaster Recovery Drill (`scripts/netci_backup.py`, `scripts/netci_dr_drill.py`)**:
  - Encrypted backup creation and decryption with PBKDF2 key derivation.
  - Automated DR drill script verifying live backup, scratch database restoration, table existence, row counts, row checksums, foreign-key integrity, and migration parity with evidence artifacts.
- See ADR-022.

### Added - Phase 8 (P1.3): Versioned Environment Configuration & DCIM Lifecycle
- **Immutable Configuration Revisions (`module_config_revisions`)**:
  - `module_config_revisions` table (migration `0014_versioned_config_revisions_and_dcim.sql`) tracking pipeline config, deployment config, change summary, status, author, approver, and rejection reasons.
  - Included in critical backup manifest and referential integrity verification (`scripts/netci_backup.py`).
- **Active Revision Pointer & Optimistic Locking (CAS)**:
  - `active_config_revision_id` and `config_version` integer on `modules`.
  - Atomic compare-and-set pointer updates preventing race conditions and silent overwrites (`409 CONCURRENT_MODIFICATION`).
- **Execution-Time Configuration Pinning**:
  - `config_revision_id` pinned on `pipeline_runs` and `deployments` at execution time, guaranteeing that in-flight or historical executions are isolated from subsequent configuration changes.
- **Change Governance & Separation of Duties**:
  - Non-production configuration changes auto-activate immediately.
  - Production changes require review and enter `pending_approval` status (`requiresApproval: True`).
  - Separation of duties strictly enforced: author proposing change cannot approve it (`403 SEPARATION_OF_DUTIES`).
- **Diff Viewer & Forward Rollback**:
  - `GET /modules/{moduleId}/config-revisions/diff` provides structural JSON path diffing between revisions.
  - `POST /modules/{moduleId}/config-revisions/{revisionNumber}/rollback` copies target revision settings into a new monotonic revision (preserving append-only history).
- **DCIM Target Host Revalidation & Health Observer**:
  - `validate_target()` verifies targets immediately before deployment dispatch, failing closed on decommissioned, maintenance, or offline servers (`422 DCIM_TARGET_UNAVAILABLE`).
  - Dynamic inventory resolution via `resolve_inventory()` replacing static host aliases.
  - `server_health_records` table and `GET /servers/health` endpoint recording live probe health without faking online status.
- **Drift Detection (`GET /modules/{moduleId}/drift`)**:
  - Detects divergence between active desired configuration revision and running deployments or live DCIM host states.
- **Portal UI (`ModulePage.tsx`)**:
  - `Configuration` tab with active revision summary, pending production approval banner, drift detection alerts, diff viewer modal, and 1-click rollback.
- See ADR-021.

### Added - Phase 7 (P1.2): Pipeline Lifecycle, Reconciliation Watchdog & Stage Events
- **Pipeline & Deployment Cancellation (`POST /pipeline-runs/{id}/cancel`, `POST /deployments/{id}/cancel`)**:
  - Direct abort of Jenkins builds (`CiLauncher.abort`) and Temporal workflows (`CdOrchestrator.cancel`).
  - Releases active environment deployment lease immediately upon cancellation.
  - SCM commit status updated to `CANCELLED`.
  - Immutable audit trail (`pipeline.cancelled`, `deployment.cancelled`).
- **Pipeline Retry with Lineage (`POST /pipeline-runs/{id}/retry`)**:
  - Guaranteed immutability of original run records.
  - Generates new run record preserving commit SHA, branch, environment, and parameters, tracking parent run via `retry_of`.
  - Prevents retry of currently active runs (`409 RUN_STILL_ACTIVE`).
  - Audited with `pipeline.retried` linking parent and child run IDs.
- **Granular Stage Events & Scoped Callbacks (`pipeline_stages` table)**:
  - Added `pipeline_stages` table (migration `0013_pipeline_lifecycle_and_stage_events.sql`) and added to backup manifest.
  - Scoped endpoints: `POST /pipeline-runs/{id}/stages` and `POST /pipeline-runs/{id}/stages/{stageId}`.
  - Scoped workload identity tokens requiring `ci:stage` or `ci:result`.
  - Idempotent upsert on `(pipeline_run_id, stage_id, attempt)`.
  - `GET /pipeline-runs/{id}/stages` for chronological stage progress and duration observation.
- **Reconciliation Watchdog Service (`backend/app/reconciler.py`)**:
  - `Reconciler` service running bounded concurrent polling (`ThreadPoolExecutor`) across active runs and deployments.
  - Auto-detection and recovery of lost Jenkins and Temporal callbacks or hung operations.
  - Automatic atomic state repair and audit logging (`pipeline.reconciled`, `deployment.reconciled`).
  - Operational endpoint `POST /reconciler/reconcile`.
- **Portal UI & Client (`netciClient.ts`, `ModulePage.tsx`)**:
  - Stage execution graph displays real-time stage status, duration, and error snippets.
  - Interactive Cancel button for running pipelines.
  - Safe Retry button with lineage display (`(retry of #...)`).
- See ADR-020.

### Added - Phase 6 (P1.1): SCM Webhook Integration, Private Repository Checkout & Commit Status
- **SCM Provider Port & Adapters (`backend/app/adapters/scm.py`)**:
  - `ScmProvider` interface with production adapters for GitHub (`GitHubScmProvider`) and GitLab (`GitLabScmProvider`), plus `MockScmProvider` for testing.
  - Cryptographic signature validation: HMAC-SHA256 (`X-Hub-Signature-256`) for GitHub, constant-time secret comparison (`X-Gitlab-Token`) for GitLab.
  - 1MB payload size enforcement (`MAX_WEBHOOK_PAYLOAD_BYTES`) on webhooks.
  - Commit status reporting back to SCM for pipeline transitions (`pending`, `running`, `success`, `failure`, `cancelled`).
- **Atomic Deduplication & Persistence (`backend/migrations/0012_scm_integrations_and_webhooks.sql`)**:
  - `scm_integrations` and `scm_webhook_deliveries` tables.
  - Atomic deduplication of webhook delivery IDs at the database level.
  - Server-managed credentialsId for private Git checkouts.
  - `console_url` added to `pipeline_runs` and exposed in API and frontend.
- **REST Endpoints & Frontend (`backend/app/main.py`, `frontend/src/ModulePage.tsx`)**:
  - `POST /applications/{id}/scm` and `GET /applications/{id}/scm` with credential redaction.
  - `POST /webhooks/scm/{provider}` for verified webhook ingestion.
  - "Open Jenkins" action enabled dynamically on `ModulePage.tsx` when `consoleUrl` is present.


### Added

- Open-source governance, contribution and security policies.
- Production Portal container and real DCIM, Jenkins, Temporal, PostgreSQL,
  Ansible and Cosign integration seams.
- Durable security evidence, production-request completion and idempotency.

### Changed

- **Truthful readiness probing and production acceptance harness.**
  Added distinct liveness (`/livez`), readiness (`/readyz`), and authenticated operator
  diagnostics (`/operator/health`) with circuit breaking, timeout protections, and safe secret
  redaction. Added `scripts/production_acceptance_harness.py` evaluating 9 critical production
  gates against live endpoints, generating immutable evidence JSON and JUnit XML reports. See ADR-018.
- **Release version immutability and append-only CI quality reports.**
  Release versions are strictly immutable and cannot overwrite artifact digests or
  provenance (`409 VersionConflict`). Quality/CI reports are stored separately in an
  append-only `version_ci_reports` table and dynamically projected. Automated backup
  and restore verification covers all 19 application tables with checksums, foreign-key
  integrity checks, and automated drills. Hardcoded mock notifications and fake unread
  badges were removed from PortalShell. See ADR-017.
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
