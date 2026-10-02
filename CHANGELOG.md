# Changelog

All notable changes to netCI will be documented here. The format follows Keep a
Changelog and releases use Semantic Versioning once the project reaches 1.0.0.

## [Unreleased]

### Added - 2026-10-02: production checks -- hung machines, untrusted builds in VMs, real monitoring
- **Hung machines** (running, answering nothing) are powered off by the supervisor through
  Redfish and taken over: 6/6 in the lab through sushy-tools' emulator, machine off 19-22 s after
  the hang, build resumed after 69-83 s (ADR-060, `chaos-poweroff-20261002T070357Z.json`).
- **Fixed:** two cells on one hung machine read as a mass failure, and the supervisor fenced
  nothing. The guard now counts machines, as Kubernetes' node controller and NHC do.
- **Untrusted builds run in their own VM**: a fabric pool with a Kata RuntimeClass
  (`lab/kata.sh`); the fabric refuses to start when a pool's RuntimeClass is missing instead of
  falling back to the shared kernel (ADR-061 superseded on that point).
- **Sandboxes' network**: a NetworkPolicy lets them reach DNS, the fabric, the controllers and
  addresses outside the cluster, nothing else; checked from both kinds of sandbox.
- **Monitoring checked against a real Prometheus and Grafana** (`lab/monitoring.sh`). Found and
  fixed: the no-leader alert stayed silent with no supervisor at all (`or absent()`); counters
  and histograms had no series before their first event, so the first lost run never alerted
  and first rates read 0. New `NetciComponentDown`; promtool unit tests for the alerts.
- **Fixed:** a `helm upgrade` that changed netci-queue's or netci-fabric's configuration did not
  restart them; their pods now carry a checksum of it.
- Lab: `FENCE=redfish` and `MONITORING=on` for `lab/helm-install.sh`; a hung-machine scenario
  (`--failure node-hang-supervised`).
- **Build logs readable during a takeover** (ADR-068): netci-cell `logShipping` copies every
  build log to Loki beside the controller; 3/3 power-offs mid-build, every line up to the crash
  readable while Jenkins was down. The Jenkins OpenTelemetry plugin was tried and reverted (10 s
  per agent, agents' output never arrived).
- **Fixed:** a JCasC file the controller refused was reported as applied (JCasC answered 200);
  `NetciCellConfigurationRefused`. A controller image rolled back left newer plugins in
  `JENKINS_HOME`; the guard now removes plugins the image does not carry. The headroom check
  counted a running build's room, and a takeover preempted the agent of the build it resumed.

### Changed - 2026-10-02: takeover in under a minute, and nothing left for a person (ADR-060, ADR-066)
- **Lab:** the latest series of 6 unattended power-offs passed, with the control plane's failover
  at upstream timings (`lab/k3s-timings.sh`). Fencing took 2.5-3.5 s and the build resumed at
  33-63 s (median 40.5 s); before, those figures were 14.5-23 s and 71-84 s. With k3s's own
  timings the build resumed at 42-83 s.
- A controller whose `JENKINS_HOME` stops taking writes is restarted by netCI, on any storage
  (ADR-067). Longhorn's own remount deletion had raced fast takeovers and deleted replacement
  controllers; StatefulSets are now left out of it.
- Fencing a machine releases the Lease of every cell on it. A second cell had waited 15 s for
  its Lease to expire.
- Longhorn's tuning is applied by admission policies (`deploy/longhorn/tuning-policy.yaml`), so
  Longhorn's driver deployer no longer undoes it. Every build
  finished, every `netciOnce` block ran once, and no log line was lost. 43 power-offs have been
  run in all.
- **Fencing.** A machine found off is fenced 3 s after its last renewal, not at Lease expiry.
  The supervisor dials the API servers directly and avoids one that failed. A gap in its
  observations no longer delays the power query.
- **Leader placement.** A leader that shares a machine with a cell hands its leadership over,
  and the replicas are re-placed as the cells move.
- **API clients** (`internal/kubeclient`) bound each attempt and drop their connections when
  one fails, so a dead API server costs one attempt, not a client's timeout.
- **Longhorn.** Its managers and CSI attachers notice a dead API server in ~4 s
  (`lab/longhorn-tune.sh`); before, they took 45 s.
- **No stuck takeovers.**
  - Controllers outrank builds through priority classes.
  - A cell with nowhere to go no longer holds its fenced machine off.
  - The supervisor reports, before any loss, a cell its machine's loss could not take over,
    and cells that share a machine.
- **Configuration.** A changed JCasC ConfigMap is applied to the running controller. A job new
  in JCasC is there from the first start: Jenkins had dropped it until a reload.
- **Fabric.** Warm sandboxes may be taken by node autoscalers; claimed ones may not.
- **Credentials.** netci-queue clients have separate permissions to trigger runs and to record
  `netciOnce` markers.
- **Operations.** `docs/RUNBOOK.md` covers every alert. The supervisor is rolled out one
  replica at a time; with a machine fenced, the rollout had waited forever.

### Added - 2026-10-01: unattended takeover verified; netciOnce (ADR-060, ADR-065)
- The Cell Supervisor ran in the lab. A VM was powered off under a running build, with no
  person involved.
  - **Run 6**: build resumed 42.5 s after the power loss.
  - Every run: SUCCESS, each step run once, at most 1 log line lost.
  - Six lab runs found and fixed six faults: see ADR-060's table.
    - SSH host-key negotiation.
    - API clients pinned to a dead API server.
    - The startup check refusing to start during an outage.
    - Longhorn waiting for NotReady.
    - Powering a machine on too early.
    - Cell leases too short for an etcd stall, which killed a healthy neighbour once.
  - Cell leases are now 15 s / 10 s. The supervisor is a hot standby.
- `netciOnce(key) { ... }` guards a block that must not run twice across a takeover. Its record
  is kept in PostgreSQL outside `JENKINS_HOME`. The guard fails closed.

### Added - 2026-10-01: agent fabric v1 (ADR-064)
- `netci-fabric` keeps warm sandbox pods and binds each one late to the controller that claims
  it. `netci-sandbox` is the sandbox's entrypoint. The netCI plugin's `netci` cloud provisions
  at once and runs one build per sandbox.
- Lab: builds run in sandboxes with a user namespace, and every claim is released. Not faster
  than the Kubernetes plugin on an idle lab with cached images (5.9 s vs 4.4 s median); see
  ADR-064.
- Controllers provision with no start-up delay. Jenkins otherwise waits 100 s after it starts,
  which a takeover would add to new builds.

### Added - 2026-10-01: durable run queue and the netCI Jenkins plugin (ADR-063)
- `netci-queue`: an intake API that accepts runs into PostgreSQL before acknowledging them,
  and dispatchers that hand each run to its cell until Jenkins has started it.
- The netCI plugin's dispatch is idempotent under Jenkins' queue lock.
- Lab: a controller was SIGKILLed under 5 queued netCI runs and 1 direct trigger. All 5 netCI
  runs ran once; the direct trigger was lost (`lab/evidence/queue-crash-*.json`).
- Redfish power control for real servers, with a per-node power-controller configuration.

### Added - 2026-10-01: cell agent and Cell Supervisor (ADR-060, decision 4)
- `cell-agent`, a sidecar: Jenkins runs only while its pod holds the cell's Lease. It is
  killed within 4 s of losing it, including when the agent itself hangs (`cell-agent guard`).
  On a graceful stop the Lease is released only after Jenkins has exited. Verified on the lab
  cluster: `lab/evidence/agent-*.json`.
- `supervisor`: fences the machine of a controller that stopped renewing its Lease, through a
  power controller (SSH forced command for libvirt in the lab), then lets Kubernetes start the
  controller elsewhere. It acts only once the machine is confirmed off. Tested against a fake
  cluster, with mutation checks; **not yet run against the lab** (`lab/supervisor.sh` changes
  the host's `authorized_keys`).
- One image, `netci/netci`, holding both binaries on `scratch`; `make image` refuses a dirty tree.

### Changed - 2026-10-01: re-architecture to Jenkins HA and an agent fabric (ADR-060..062)
- netCI becomes a high-availability layer and an agent fabric around the organisation's own
  open-source Jenkins, which keeps doing CI and CD. Services are rewritten in Go.
- Removed: the portal, the Python backend, Temporal CD and deployment runtimes, the service
  catalog, Backstage integration, sample apps, the kind-based corp lab and the old evidence.
  They remain at the tag `netci-0.3-cd-portal`. Guides for them moved to `docs/archive/`.

### Changed - 2026-09-29: shared pipelines, toolchain governance, portal
- **Shared CI pipelines (ADR-058, amended 2026-09-29):** one script per pipeline, cut into stages by
  `# @stage` markers; built-in blocks run the library's code with scoped credentials, author blocks
  run without any. Creating a pipeline or saving a version takes effect immediately for developer,
  reviewer and platform-admin; only `build` and `publish` are required. Runs pin name, version and
  sha256; launch re-hashes the script. Migration `0035_shared_pipelines.sql`.
- **Jenkins controller and plugins declared (ADR-059):** `toolchain/versions.yaml` pins the
  controller base by digest and all 85 plugins; `plugins.txt` is generated
  (`scripts/toolchain_sync.py`); a controller with drifted or unreadable plugins takes no build.
  Controller moved to Jenkins 2.555.3 LTS (fixes SECURITY-3672/3790/3796/3815/3729).
- **Portal:** Pipelines page (designer, Jenkinsfile preview, version history), new-module wizard
  picks a pipeline by name, Release Calendar month/week/list views, Service Catalog explanations,
  UI in English; removed the Golden Path Templates tab and the Vulnerabilities page.
- **Corp lab:** node addresses pinned (`scripts/corp/pin_node_ips.sh`) after a reboot broke etcd
  quorum; library `netci-0.4.2`; images `0.3.0-corp4`.

### Added - Phase 13 (P2.4): Platform Integrity, Clean-Room Verification & Final Production Readiness Certification
- **Automated Production Readiness Audit Suite (`scripts/production_readiness_audit.py`)**:
  - Implemented end-to-end verification covering all 13 architecture phases across 28 distinct invariant checks.
  - Generates machine-readable audit report at `evidence/production_readiness_audit.json` with 100% pass rate (28/28 checks PASS) and `CERTIFIED` verdict.
- **Clean-Room Enforcement & Fail-Closed Integrity**:
  - Validated zero mock/fake data fallbacks in default runtime paths.
  - Enforced fail-closed behavior across all external dependencies (PostgreSQL, DCIM, Cosign, Secrets/Vault).
- **Comprehensive Operational Documentation & Architecture Decisions**:
  - Added `docs/LIVE-READINESS.md` detailing operational requirements, invariant gates, and verification runbooks.
  - Added `docs/decisions/ADR-026-production-readiness-and-certification.md` defining platform certification standards.
  - Updated `docs/HUONG-DAN-HIEU-TOAN-BO-NETCI.md` architectural corpus reflecting full project maturity.
- See ADR-026.

### Added - Phase 12 (P2.3): Service Catalog, Golden Path Templates, Ephemeral Preview Environments & Self-Service Developer Workflows
- **Canonical PostgreSQL Schema Migration 0018 (`backend/migrations/0018_service_catalog_and_self_service.sql`)**:
  - `catalog_services`: Authoritative software entities with tier (`tier-1`, `tier-2`, `tier-3`), lifecycle state (`active`, `deprecated`, `decommissioned`), owning team, repo/docs URLs, and metadata.
  - `catalog_service_dependencies`: Directional upstream/downstream dependency declarations with synchronous, asynchronous, and database dependency types.
  - `catalog_templates`: Versioned Golden Path pipeline templates with category, parameters schema, and pipeline definitions.
  - `preview_environments`: Isolated ephemeral pull-request environments with namespace synthesis, URL generation, TTL boundaries (1h to 72h), and status tracking (`active`, `expired`, `destroyed`).
  - `resource_requests`: Self-service cloud infrastructure requests with dual-control governance, environment boundaries, and fail-closed provider integration.
- **Service Catalog & Dependency Graph (`backend/app/catalog/services.py`)**:
  - `CatalogServiceManager`: Tier and lifecycle validation, ownership enforcement, and Depth-First Search (DFS) cycle detection to prevent circular dependency deadlocks.
- **Golden Path Pipeline Template Engine (`backend/app/catalog/templates.py`)**:
  - `PipelineTemplateEngine`: Semver compliance validation, JSON schema parameter checking, parameter interpolation into pipeline commands (`${parameters.KEY}`), and pre-seeded production templates (`fastapi-service`, `go-microservice`, `react-spa`).
- **Ephemeral Preview Environment Manager (`backend/app/catalog/previews.py`)**:
  - `PreviewEnvironmentManager`: RFC 1123 compliant namespace generation, preview ingress URLs, TTL lease validation, and automated reconciliation.
- **Governed Self-Service Infrastructure Manager (`backend/app/catalog/resources.py`)**:
  - `SelfServiceResourceManager`: Fail-closed provider contract (`provider_not_configured` when external provider driver is not registered), credential redaction in outputs, and strict separation-of-duties dual control (`approved_by != requested_by` on staging/production).
- **REST Endpoints & Contract Parity (`backend/app/main.py`, `api/openapi.yaml`)**:
  - 21 new endpoints spanning catalog CRUD, dependency graph inspection, template instantiation, preview lifecycle, and self-service resources.
  - 100% OpenAPI 3.1.0 contract parity verified in `tests/contract/test_openapi.py`.
- **Frontend Portal Integration (`frontend/src/CatalogPage.tsx`, `frontend/src/api/netciClient.ts`)**:
  - Interactive multi-tab developer portal view for Services, Golden Path Templates, Ephemeral Previews, and Self-Service Resources.
  - 1-click template instantiation modal generating complete pipeline and deployment configuration plans.
  - Preview environment list with real-time TTL expiration counters, namespace links, and teardown actions.
  - Resource request dashboard with status badges, status reasons, and dual-control approval enforcement.
  - Full Vitest test suite and TypeScript production build pass.
- See ADR-025.

### Added - Phase 11 (P2.2): Enterprise Governance, Policy Engine, Security Exceptions, Break-Glass Dual Control, Quotas & Kubernetes Admission Control
- **Database Schema Migration 0017 (`backend/migrations/0017_policy_engine_governance_and_admission.sql`)**:
  - `policy_decisions`: Durable records of all evaluated rules, checks, risk scores, and reasons with target/cursor indexing.
  - `security_exceptions`: Time-boxed, CVE-bound vulnerability waivers pinned to immutable sha256 artifact digests with owner/approver dual control.
  - `break_glass_requests`: Two-person emergency bypass mechanism requiring incident tickets and short-lived leases (TTL <= 4h).
  - `resource_quotas`: Scope-based (team, application, global) concurrency and rate limits for pipelines and deployments.
- **Enterprise Policy Engine & Risk Scoring (`backend/app/policy/`)**:
  - `RiskCalculator`: Deterministic 0-100 deployment risk score based on target environment, blast radius, test automation coverage, active CVE waivers, rollback strategy, and break-glass invocation.
  - `QuotaEnforcer`: Concurrency checking across pipeline runs and deployment executions against hierarchical quotas.
  - `BreakGlassService`: Dual-control emergency override service strictly enforcing separation of duties (`requested_by != approved_by`).
  - `PolicyEngine` & `BuiltinPolicyEngine`: Durable evaluation and audit recording for artifact admission, production approvals, and deployment gates.
- **Kubernetes Dynamic Admission Controller (`backend/app/admission.py`)**:
  - `AdmissionController` validating `AdmissionReview` v1 webhooks.
  - Rejects unpinned mutable image tags (e.g. `:latest`) in production namespaces.
  - Verifies container images against stored Syft SBOM, Trivy scan, and Cosign signature evidence.
  - Permits emergency admission when covered by an active break-glass request.
- **REST API & Contract Parity (`backend/app/main.py`, `api/openapi.yaml`)**:
  - `GET /policy/decisions`: Paginated policy decisions.
  - `GET /security-exceptions`, `POST /security-exceptions`, `POST /security-exceptions/{id}/revoke`: Security waiver lifecycle management.
  - `POST /break-glass/requests`, `POST /break-glass/requests/{id}/approve`, `GET /break-glass/active`: Two-person break-glass emergency flow.
  - `GET /quotas/{scope}/{scopeId}`, `PUT /quotas/{scope}/{scopeId}`: Resource quota management.
  - `POST /admission/validate`: Dynamic Kubernetes admission review endpoint.
  - Verified 100% parity against OpenAPI 3.1.0 contract tests (`tests/contract/test_openapi.py`).
- **Portal UI Enhancement (`ProductionRequestsPage.tsx`, `netciClient.ts`)**:
  - Extended API client with governance, exception, quota, and break-glass types and functions.
  - Added Enterprise Governance & Policy Verification card to Request Details modal with real-time risk score badge, dual control status, and break-glass indicators.
  - Vitest test suite and TypeScript production build passing without error.
- See ADR-024.

### Added - Phase 10 (P2.1): Multi-Module DAG Release Plan, SAGA Orchestration & Progressive Delivery
- **Topological DAG Wave Computation (`backend/app/domain/dag.py`)**:
  - Implemented Kahn's algorithm with wave layering (`compute_dag_waves`).
  - Automatically groups independent modules into concurrent deployment waves.
  - Cycle detection rejecting circular dependencies with 422 `CYCLIC_DEPENDENCY`.
  - Comprehensive unit test coverage (`backend/tests/test_dag.py`).
- **Database Schema Migration 0016 (`backend/migrations/0016_multi_module_dag_and_progressive_delivery.sql`)**:
  - `production_requests`: added `release_plan JSONB`, `strategy VARCHAR(32)`, `strategy_config JSONB`.
  - `production_request_modules`: added `dependencies TEXT[]`, `status VARCHAR(32)`, `deployment_id UUID`, `started_at`, `completed_at`, `error_message`.
  - `deployments`: added `strategy VARCHAR(32)`, `traffic_weight INTEGER`, `active_color VARCHAR(16)`, `canary_step INTEGER`.
  - Added indexes for coordinator lookups and status filtering.
- **SAGA Release Plan Coordinator (`backend/app/coordinator.py`)**:
  - Wave-by-wave deployment dispatch on production request approval.
  - Automatic wave advancement as modules pass health checks.
  - SAGA reverse rollback compensation: when any module in a wave fails, automatically triggers rollbacks of earlier waves in reverse topological order, setting request to `blocked`/`rejected` and modules to `rolled_back`.
- **Progressive Delivery & Traffic Engine (`backend/app/traffic.py`)**:
  - `TrafficRoutingAdapter` and `InMemoryTrafficRoutingAdapter` with weighted split and blue/green active color management.
  - `CanaryAnalyzer` evaluating SLO thresholds (`maxErrorRate`, `maxP95LatencyMs`).
  - Canary progression management: `POST /production-requests/{id}/canary/advance` and `POST /production-requests/{id}/canary/abort`.
- **OpenAPI 3.1.0 Contract Parity**:
  - Extended OpenAPI definitions for multi-module requests, strategies (`rolling`, `canary`, `blue_green`), release plan endpoints, and traffic endpoints (`/deployments/{id}/traffic`).
  - Passed all contract tests in `tests/contract/test_openapi.py`.
- **Portal UI Enhancement (`ProductionRequestsPage.tsx`, `netciClient.ts`)**:
  - Multi-module selection wizard with dependency checkboxes.
  - Strategy selector (Rolling DAG, Canary Rollout, Blue/Green).
  - Visual DAG release plan waves in request details modal.
  - Interactive canary control panel with live traffic weight % and advance/abort actions.
  - Frontend test coverage in `ProductionRequestsPage.test.tsx` and full Vitest suite passing.
- See ADR-023.

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
