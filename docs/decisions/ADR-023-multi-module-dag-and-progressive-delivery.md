# ADR-023: Multi-Module Release Plan DAG Orchestration, SAGA Compensation, and Progressive Delivery

## Status
Accepted

## Context
In large enterprise environments (P2.1), microservice and distributed system deployments rarely occur in isolation. Single-module deployments forced operators to manually sequence multiple interdependent releases across databases, APIs, workers, and frontends, leading to severe operational risks:
1. **Lack of Dependency-Aware Scheduling**: Interdependent services (e.g. `web-app` depending on `api-service`, which in turn depends on `db-service`) had to be triggered manually. If an operator deployed out of order, downstream services immediately crashed or entered split-brain states.
2. **Missing Multi-Wave Orchestration**: No topological sort existed to analyze module dependency graphs, discover cycles (`CYCLIC_DEPENDENCY`), and layer deployments into parallel or sequential execution waves.
3. **Absence of SAGA Rollback Compensation**: When a downstream module failed in a multi-service release, previously deployed upstream modules remained in production without automated rollback or compensation, leaving the system in an inconsistent, partially updated state.
4. **Binary All-or-Nothing Deployments**: Services lacked progressive delivery traffic routing strategies (Canary traffic stepping with threshold evaluations, and Blue/Green instant cutover with zero downtime).
5. **UI & API Disconnect**: The frontend Portal explicitly blocked multi-module selections and lacked visual DAG wave progression tracking and progressive canary control actions.

## Decision

1. **Topological DAG Wave Computation (`backend/app/domain/dag.py`)**:
   - Implemented Kahn's topological sort algorithm with wave layering (`compute_dag_waves`).
   - Automatically clusters modules whose dependencies are satisfied into concurrent execution waves:
     - Wave 1: independent modules (e.g. `db-service`).
     - Wave 2: modules whose dependencies are satisfied by Wave 1 (e.g. `api-service`).
     - Wave 3: edge services (e.g. `web-app`).
   - Rejects circular dependencies with deterministic 422 `CYCLIC_DEPENDENCY` errors.
   - Validates undeclared or external dependency references with 422 `UNKNOWN_DEPENDENCY`.

2. **Schema Migration 0016 (`backend/migrations/0016_multi_module_dag_and_progressive_delivery.sql`)**:
   - `production_requests`: added `release_plan JSONB`, `strategy VARCHAR(32)` (rolling, canary, blue_green), and `strategy_config JSONB`.
   - `production_request_modules`: added `dependencies TEXT[]`, `status VARCHAR(32)`, `deployment_id UUID`, `started_at TIMESTAMPTZ`, `completed_at TIMESTAMPTZ`, and `error_message TEXT`.
   - `deployments`: added `strategy VARCHAR(32)`, `traffic_weight INTEGER`, `active_color VARCHAR(16)`, and `canary_step INTEGER`.
   - Added indexes on `(request_id, status)`, `deployment_id`, and `strategy`.

3. **SAGA Release Plan Coordinator (`backend/app/coordinator.py`)**:
   - Orchestrates multi-module releases: on production request approval, dispatches Wave 1 modules and links their deployments.
   - Listens to deployment lifecycle result callbacks: when all modules in a wave succeed (`status: healthy`), automatically dispatches the next wave.
   - **SAGA Reverse Compensation**: On module health check failure (`status: failed`), halts subsequent waves and rolls back previously succeeded modules in reverse topological order (`_handle_wave_failure`), setting statuses to `rolled_back` and the request to `blocked` or `rejected`.

4. **Progressive Delivery & Traffic Routing Engine (`backend/app/traffic.py`)**:
   - Created `TrafficRoutingAdapter` and `InMemoryTrafficRoutingAdapter` for weighted traffic splits and blue/green host header routing.
   - Built `CanaryAnalyzer` evaluating metrics (`maxErrorRate`, `maxP95LatencyMs`) against configured SLO thresholds.
   - Implemented `advance_canary` (advancing steps, e.g. 10% -> 25% -> 50% -> 100%) and `abort_canary` (instant rollback to baseline and zero traffic).

5. **REST API & Contract Synchronization (`backend/app/main.py`, `api/openapi.yaml`)**:
   - Multi-module request support in `POST /production-requests` with `dependencies`, `strategy`, and `strategyConfig`.
   - Added endpoints:
     - `GET /production-requests/{requestId}/plan`: retrieves computed DAG wave release plan and per-module execution statuses.
     - `POST /production-requests/{requestId}/canary/advance`: advances canary step after evaluating traffic metrics.
     - `POST /production-requests/{requestId}/canary/abort`: aborts active canary and initiates rollback.
     - `GET /deployments/{deploymentId}/traffic`: queries current traffic allocation, active color, and canary step.
   - Verified 100% parity against OpenAPI 3.1.0 contract tests (`tests/contract/test_openapi.py`).

6. **Release Portal UI (`frontend/src/ProductionRequestsPage.tsx`, `frontend/src/api/netciClient.ts`)**:
   - Enhanced request creation wizard: multi-module selection, dependency assignment checkboxes, and strategy selection (`Rolling DAG`, `Canary Rollout`, `Blue / Green`).
   - Enhanced Request Details modal:
     - Real-time DAG Release Plan wave cards and per-module status pills.
     - Interactive Canary control panel displaying live traffic weight %, current step, and "Advance Step" / "Abort Canary" actions.
     - Strategy badge in the production requests list table.

## Consequences
- Multi-module systems can be safely promoted with guaranteed dependency-ordered deployment waves.
- Failures in multi-module releases automatically trigger reverse SAGA rollbacks, preventing partial or broken production states.
- High-risk services can leverage canary traffic shifting or blue/green instant cutover with automated SLO gating.
- Full parity across database schema, backend coordinator, OpenAPI contract, and web UI.
