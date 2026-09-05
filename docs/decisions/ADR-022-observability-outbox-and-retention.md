# ADR-022: Observability, Transactional Outbox, Connection Pooling, and Disaster Recovery

## Status
Accepted

## Context
As netCI transitions to long-term reliable production operation (P1.4), several critical reliability and operational gaps needed resolution:
1. **Unbounded Database Connections & Concurrency Bottlenecks**: Previously, each transaction or request opened a new ephemeral psycopg connection. Under load spikes, this exhausted PostgreSQL socket limits and returned unhandled errors rather than queued, bounded access.
2. **Missing Correlation Context & Sensitive Credential Leakage in Logs**: Unstructured text logs lacked request correlation IDs across distributed microservices (API -> Outbox -> SCM -> Workers), and could inadvertently leak JWTs, private tokens, or authorization headers into stdout.
3. **Lack of Standard Metrics Exposition**: Prometheus-format metrics were missing for request durations, status code counts, connection pool utilization, and delivery outbox depths.
4. **Non-Transactional Notification Delivery & Dual-Write Hazard**: Emitting webhook or notification events synchronously during HTTP requests risks partial failure: if the notification succeeds but the DB transaction rolls back (or vice versa), notification state diverges from reality.
5. **Pagination Inefficiencies & O(N) Offset Overhead**: Listing pipelines, deployments, audit records, and notifications used unbounded queries or offset-based pagination that degrades severely as tables grow into millions of rows.
6. **Data Bloat & Lack of Lifecycle Retention**: Ephemeral records (such as single-use callback tokens, delivered notifications, and webhook delivery records) accumulated indefinitely without automated TTL or purge policies.
7. **Disaster Recovery Gap**: Backups existed, but lacked AES-256-GCM encryption at rest and automated end-to-end restore-and-verify drills ensuring referential integrity and zero data loss.

## Decision

1. **Thread-Safe PostgreSQL Connection Pooling (`backend/app/store/postgres.py`)**:
   - Implemented `PostgresConnectionPool` with configurable pool sizing (`NETCI_DB_POOL_MIN`, `NETCI_DB_POOL_MAX`), acquisition timeouts (`TimeoutError`), connection liveness validation (`SELECT 1`), idle statistics, and graceful disposal.
   - Refactored `PostgresDatabase.transaction()` to acquire a connection from the pool and return it immediately upon commit or rollback.

2. **Structured JSON Logging and Sensitive Redaction (`backend/app/logging.py`)**:
   - Standardized all application logging to `StructuredJsonFormatter`, outputting structured JSON with `timestamp`, `level`, `service`, `correlation_id`, `caller`, and message.
   - Built automatic credential redactors stripping Bearer tokens, SCM secrets, passwords, and private keys (`[REDACTED]`).
   - Wired `current_correlation_id` ContextVar to `correlation_id_middleware` in `backend/app/main.py`, propagating through headers (`X-Correlation-ID`) and background tasks.

3. **Prometheus Metrics Exposition (`backend/app/metrics.py`)**:
   - Created in-process thread-safe `MetricsRegistry` collecting:
     - `netci_http_requests_total` (counter, labeled by method, path template, status code).
     - `netci_http_request_duration_seconds` (histogram with standard latency buckets).
     - `netci_notifications_outbox_depth` (gauge for pending notifications).
     - `netci_db_pool_active` and `netci_db_pool_idle` (gauges for connection pool capacity).
   - Exposed standard `/metrics` endpoint returning Prometheus text exposition format.

4. **Transactional Outbox for Notifications (`backend/app/notifications.py`, migration `0015`)**:
   - Created `notifications` table with status enum (`pending`, `delivered`, `failed`, `dead_letter`), retry attempts, max attempts (5), exponential backoff schedule (2s, 4s, 8s, 16s, 32s), and indexed payload.
   - Added `UnitOfWork.notifications` queue: notification records are inserted atomically within the exact same database transaction as the business event (pipeline trigger, deployment completion, config approval).
   - Built background `NotificationOutboxWorker` polling `pending` items, delivering them via HTTP webhooks, and routing exhausted items to `dead_letter` queue.
   - Added management endpoints: `GET /notifications` (cursor-paginated) and `POST /notifications/{id}/retry`.

5. **Cursor-Based Pagination**:
   - Implemented monotonic cursor-based pagination for `GET /notifications`, `GET /pipeline-runs`, `GET /deployments`, and `GET /audit-events` using `(created_at, id)` composite tuples.
   - Added corresponding compound B-Tree indexes in migration `0015` (`idx_pipeline_runs_cursor`, `idx_deployments_cursor`, `idx_audit_events_cursor`, `idx_notifications_cursor`).
   - Query responses include `items`, `limit`, and opaque `nextCursor` for O(1) retrieval regardless of table depth.

6. **Retention Purge Policies & CLI (`backend/app/retention.py`, `scripts/netci_retention_purge.py`)**:
   - Added purge routines for expired callback tokens (`callback_token_uses` past `expires_at`), delivered notifications older than retention window, and SCM webhook deliveries.
   - Exposed `POST /admin/retention/purge` and standalone CLI script `scripts/netci_retention_purge.py`.

7. **AES-256-GCM Encrypted Backups & Automated Disaster Recovery Drill (`scripts/netci_backup.py`, `scripts/netci_dr_drill.py`)**:
   - Extended `netci_backup.py` to support AES-256-GCM authenticated encryption (`--encrypt`, `--encryption-key`, salt + nonce + ciphertext).
   - Automated DR drill script `scripts/netci_dr_drill.py` that takes a live encrypted backup, verifies checksums, restores into an isolated scratch database, verifies all 34 critical tables across all 18 migrations, executes row-count and row-checksum comparisons, verifies zero foreign-key orphan violations, checks migration list completeness, and outputs a timestamped evidence artifact in `evidence/dr_drill_*.json`.

## Consequences
- Guarantees at-least-once notification delivery with zero dual-write inconsistencies.
- Eliminates connection starvation and socket resource leakage.
- Enables complete observability via Prometheus scraping and structured JSON log aggregators.
- Fully verifies disaster recovery readiness on live PostgreSQL instances.
