# ADR-025: Service Catalog, Golden Path Templates, Ephemeral Preview Environments, and Self-Service Developer Workflows

## Status
Accepted

## Context
As the netCI Delivery Platform scales to multi-team enterprise operations (Phase 12 / P2.3), development teams require standard golden path workflows and autonomous self-service capabilities without compromising security, architecture standards, or governance:

1. **Authoritative Software Catalog & Dependency Topology**:
   - Microservices across teams frequently suffer from orphaned ownership, unclear tiers (criticality vs supporting), and unmapped dependencies.
   - Circular service dependencies cause runtime startup deadlocks, cascading failures, and distributed release deadlocks.
   - A centralized, queryable service catalog is required with authoritative team ownership, tier classification (`tier-1`, `tier-2`, `tier-3`), lifecycle state (`active`, `deprecated`, `decommissioned`), and automated cycle detection on upstream/downstream dependency declarations.

2. **Standardized "Golden Path" Pipeline Templates**:
   - Teams writing raw pipeline definitions from scratch introduce drift, security vulnerabilities, and brittle build configurations.
   - Versioned Golden Path templates (e.g. `fastapi-service`, `go-microservice`, `react-spa`) must be parameterized, validated against JSON schemas, and instantiated into full runtime configurations with single-click developer workflows.

3. **Ephemeral Preview Environments for Pull Requests**:
   - Testing changes in shared static environments creates contention, dirty state, and deployment bottlenecks.
   - Developers need automated, isolated preview environments provisioned per pull request with unique DNS namespaces (e.g. `https://pr42.preview.delivery.corp`), bounded Time-To-Live (TTL between 1 hour and 72 hours, default 24 hours), and automatic expiration reconciliation.

4. **Governed Infrastructure Self-Service with Separation of Duties**:
   - Developers frequently need cloud and platform resources (databases, redis caches, S3 storage buckets, IAM roles) for development, preview, and production.
   - Traditional ticket-based IT provisioning is slow and manual. Conversely, unregulated self-service risks security breaches, budget overruns, and unmanaged resources.
   - A governed self-service resource workflow must enforce:
     - Strict separation of duties on staging and production: self-approval is forbidden (`approved_by != requested_by`), returning HTTP 403 `SEPARATION_OF_DUTIES`.
     - Deterministic outputs without leaking plaintext credentials.
     - Fail-closed behavior: requests must transition to `provider_not_configured` when an external cloud provider driver is absent, completely forbidding fake successes in production code.

---

## Decision

1. **Canonical PostgreSQL Schema (Migration 0018 - `backend/migrations/0018_service_catalog_and_self_service.sql`)**:
   - `catalog_services`: `(id, name, description, owning_team, tier, lifecycle, repo_url, docs_url, metadata, created_at, updated_at)`.
   - `catalog_service_dependencies`: `(id, source_service_id, target_service_id, dependency_type, description, created_at)` with `UNIQUE(source_service_id, target_service_id)`.
   - `catalog_templates`: `(id, version, name, description, category, parameters_schema, pipeline_definition, is_deprecated, created_at, updated_at)` with `PRIMARY KEY (id, version)`.
   - `preview_environments`: `(id, application_id [UUID], pull_request_id, commit_sha, namespace, url, status, ttl_seconds, expires_at, created_by, created_at, destroyed_at)` with `REFERENCES applications(id)`.
   - `resource_requests`: `(id, application_id [UUID], team_id, environment, resource_type, spec, status, status_reason, provider, outputs, requested_by, approved_by, created_at, updated_at)` with `REFERENCES applications(id)`.
   - Added indexes on `owning_team`, `tier`, `lifecycle`, `application_id`, `status`, `expires_at`, and cursor columns.

2. **Domain Architecture (`backend/app/catalog/`)**:
   - `services.py`: `CatalogServiceManager` enforcing valid tiers (`tier-1`, `tier-2`, `tier-3`), lifecycles (`active`, `deprecated`, `decommissioned`), non-empty ownership, and Depth-First Search (DFS) cycle detection across directed dependency graphs.
   - `templates.py`: `PipelineTemplateEngine` enforcing semver compliance (`major.minor.patch`), JSON schema parameter validation, regex parameter interpolation (`${parameters.KEY}`), and seeding built-in production templates (`fastapi-service`, `go-microservice`, `react-spa`).
   - `previews.py`: `PreviewEnvironmentManager` synthesizing sanitized RFC 1123 namespaces, generating ingress URLs, enforcing TTL boundaries (1h min, 24h default, 72h max), and executing background expiration reconciliation.
   - `resources.py`: `SelfServiceResourceManager` enforcing dual-control approvals on staging/production, fail-closed provider contract (`provider_not_configured` when external provider driver is not registered), and safe output redaction.

3. **REST Endpoints & 100% OpenAPI 3.1.0 Parity (`backend/app/main.py`, `api/openapi.yaml`)**:
   - `/catalog/services`: CRUD and cursor pagination for service records.
   - `/catalog/services/{serviceId}/dependencies`: Declarative dependency topology management and cycle analysis.
   - `/catalog/templates`: Registration, listing, and 1-click instantiation into application configs.
   - `/preview-environments`: Lifecycle management, URL inspection, and teardown for ephemeral environments.
   - `/self-service/resources`: Creation, dual-control approval, inspection, and deprovisioning for infrastructure resources.
   - Full OpenAPI 3.1.0 parity verified against `tests/contract/test_openapi.py`.

4. **Frontend Developer Portal (`frontend/src/CatalogPage.tsx`, `frontend/src/api/netciClient.ts`)**:
   - Added dedicated Service Catalog & Self-Service Portal view accessible from primary navigation.
   - Tab 1: **Services & Dependency Graph** with tier tags, lifecycle status, search, and upstream/downstream inspector with circular dependency alerts.
   - Tab 2: **Golden Path Templates** featuring 1-click instantiation modal generating full pipeline and deployment configuration plans.
   - Tab 3: **Ephemeral Preview Environments** displaying active PR namespaces, URLs, TTL expiration timers, and teardown actions.
   - Tab 4: **Self-Service Resources** displaying resource status badges, status reasons, provider outputs, and dual-control approval controls.

---

## Consequences
- Every platform service has authoritative metadata and verified dependency topology, eliminating cyclic release deadlocks.
- Standard Golden Path templates allow developers to bootstrap production-ready microservices in seconds.
- Ephemeral preview environments enable high-velocity PR reviews without manual cleanup overhead.
- Self-service cloud resource requests are governed by strict separation of duties and auditable PostgreSQL persistence without mock shortcuts.
