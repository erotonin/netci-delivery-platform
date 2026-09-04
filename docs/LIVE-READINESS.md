# netCI Production Readiness & Operational Verification Guide

This document details the operational standards, invariant enforcement, and execution instructions for the **netCI Production Readiness & Certification Harness** (Phases 1 through 13).

---

## 1. Executive Summary & Philosophy

The netCI Delivery Platform is designed under strict enterprise zero-trust and fail-closed operational principles:
- **No False Greens**: An unconfigured external dependency or service must never report `healthy` or return synthetic successful results. It reports `not_configured` or fails closed.
- **Canonical PostgreSQL Persistence**: All mission-critical domain entities, deployments, pipelines, leases, and audit trails reside in PostgreSQL with forward-only versioned migrations.
- **Atomic, Idempotent, and Fenced State Transitions**: State machines cannot skip mandatory operational phases (e.g. `QUEUED` cannot jump directly to `SUCCEEDED`; `PENDING_APPROVAL` cannot jump to `HEALTHY`). Rollbacks require a distinct `ROLLBACK_IN_PROGRESS` phase.
- **Dual-Control Governance**: Sensitive operations (break-glass requests, production self-service resource provisioning) strictly disallow self-approval (`requested_by != approved_by`).
- **Cryptographic Immutability**: All release artifacts, admission controller reviews, and SCM webhooks require cryptographic digests (@sha256:...) and HMAC-SHA256 signature verification.

---

## 2. Executing the Production Readiness Audit

The unified audit suite validates all 28 mission-critical invariant gates across all 13 phases.

### Quick Run
```bash
# Point to canonical PostgreSQL instance
DATABASE_URL="postgresql://netci:netci-local-only@127.0.0.1:55432/netci" \
python scripts/production_readiness_audit.py
```

### Generated Evidence
Upon completion, the audit writes machine-readable evidence to:
```
evidence/production_readiness_audit.json
```
This JSON file contains:
- Total check count and pass rate (28/28 = 100%)
- Overall verdict: `CERTIFIED`
- Granular per-phase breakdown and timestamps.

---

## 3. The 13 Phases of Production Readiness

| Phase | Description | Key Invariant Checks |
|---|---|---|
| **Phase 1** | PostgreSQL Canonical State & Migrations | Exactly 18 SQL migrations; all 34 canonical domain tables verified in live database. |
| **Phase 2** | Workload Identity & Input Boundaries | Scoped machine tokens; Jenkins denied `deployment:result`; pipeline inputs stripped of deployment-controlled keys. |
| **Phase 3** | Leases, Fencing & State Transitions | Strict state machines; two-phase rollback (`ROLLBACK_IN_PROGRESS` -> `ROLLED_BACK`). |
| **Phase 4** | Release Immutability & Clean-Room | Zero mock/fixture adapters in runtime defaults; DCIM fail-closed catalog. |
| **Phase 5** | Truthful Readiness & Dependency Circuit Breakers | DCIM, Cosign, and Secrets report `not_configured` when unconfigured without false green. |
| **Phase 6** | SCM Webhooks & HMAC Verification | GitHub/GitLab webhook signature verification and rejection of forged signatures. |
| **Phase 7** | Pipeline Lifecycle & Reconciler | Asynchronous state reconciliation watchdog for orphaned or interrupted pipeline runs. |
| **Phase 8** | Versioned Config & DCIM Target Revalidation | Dynamic target revalidation fails closed if DCIM backend becomes unreachable. |
| **Phase 9** | Observability, Metrics & Outbox | Prometheus `MetricsRegistry` exposition of HTTP counters, latencies, and deployment metrics. |
| **Phase 10** | DAG Release Plans & Progressive Delivery | Kahn's algorithm for parallel waves; cycle detection; Canary SLO evaluation. |
| **Phase 11** | Enterprise Governance & Admission Control | Deterministic risk scoring (0-100); break-glass dual-control enforcement; Kubernetes admission webhook rejecting `:latest` tags. |
| **Phase 12** | Service Catalog & Self-Service | Service catalog dependency cycle detection; Golden Path templates; preview environment TTL bounding (max 72h); fail-closed self-service resources. |
| **Phase 13** | Platform Integrity & Certification | OpenAPI 3.1.0 contract verification; release checklist; comprehensive ADR corpus (>= 25 records). |

---

## 4. Disaster Recovery & Backup Verification

Periodic DR drills ensure recovery point objectives (RPO) and recovery time objectives (RTO):
```bash
# Execute disaster recovery drill with checksum verification
python scripts/netci_backup.py drill --target-dir backups/dr-drill
```
Drill records are archived in `evidence/dr_drill_*.json`.
