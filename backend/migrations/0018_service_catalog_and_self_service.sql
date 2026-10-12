-- 0018_service_catalog_and_self_service.sql
-- Phase 12 (P2.3): Service Catalog, Golden Path Templates, Ephemeral Preview Environments & Self-Service Workflows

-- 1. Catalog Services: Central registry of services with first-class owning team, tier, and lifecycle
CREATE TABLE IF NOT EXISTS catalog_services (
    id VARCHAR(128) PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    owning_team VARCHAR(128) NOT NULL,
    tier VARCHAR(32) NOT NULL DEFAULT 'tier-2',
    lifecycle VARCHAR(32) NOT NULL DEFAULT 'active',
    repo_url TEXT NOT NULL DEFAULT '',
    docs_url TEXT NOT NULL DEFAULT '',
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_catalog_services_team ON catalog_services (owning_team);
CREATE INDEX IF NOT EXISTS idx_catalog_services_tier_lifecycle ON catalog_services (tier, lifecycle);
CREATE INDEX IF NOT EXISTS idx_catalog_services_cursor ON catalog_services (created_at DESC, id DESC);

-- 2. Service Dependencies: Directed dependency graph between catalog services
CREATE TABLE IF NOT EXISTS catalog_service_dependencies (
    id UUID PRIMARY KEY,
    source_service_id VARCHAR(128) NOT NULL REFERENCES catalog_services(id) ON DELETE CASCADE,
    target_service_id VARCHAR(128) NOT NULL REFERENCES catalog_services(id) ON DELETE CASCADE,
    dependency_type VARCHAR(32) NOT NULL DEFAULT 'sync',
    description TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_service_dependency UNIQUE (source_service_id, target_service_id)
);

CREATE INDEX IF NOT EXISTS idx_service_deps_source ON catalog_service_dependencies (source_service_id);
CREATE INDEX IF NOT EXISTS idx_service_deps_target ON catalog_service_dependencies (target_service_id);

-- 3. Catalog Templates: Golden path pipeline and scaffolding templates with versioning and JSON schema inputs
CREATE TABLE IF NOT EXISTS catalog_templates (
    id VARCHAR(128) NOT NULL,
    version VARCHAR(32) NOT NULL,
    name VARCHAR(255) NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    category VARCHAR(64) NOT NULL DEFAULT 'backend',
    parameters_schema JSONB NOT NULL DEFAULT '{}'::jsonb,
    pipeline_definition JSONB NOT NULL DEFAULT '{}'::jsonb,
    is_deprecated BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (id, version)
);

CREATE INDEX IF NOT EXISTS idx_catalog_templates_category ON catalog_templates (category);
CREATE INDEX IF NOT EXISTS idx_catalog_templates_deprecated ON catalog_templates (is_deprecated);

-- 4. Preview Environments: PR-driven ephemeral environments with isolated namespace, URL, and bounded TTL
CREATE TABLE IF NOT EXISTS preview_environments (
    id VARCHAR(128) PRIMARY KEY,
    application_id UUID NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
    pull_request_id VARCHAR(64) NOT NULL,
    commit_sha VARCHAR(64) NOT NULL,
    namespace VARCHAR(128) NOT NULL,
    url TEXT NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'pending',
    ttl_seconds INTEGER NOT NULL DEFAULT 86400,
    expires_at TIMESTAMPTZ NOT NULL,
    created_by VARCHAR(128) NOT NULL DEFAULT 'system',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    destroyed_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_preview_environments_app ON preview_environments (application_id);
CREATE INDEX IF NOT EXISTS idx_preview_environments_active_expiry ON preview_environments (status, expires_at);

-- 5. Self-Service Resource Requests: Developer self-service provisioning with strict fail-closed provider contract
CREATE TABLE IF NOT EXISTS resource_requests (
    id UUID PRIMARY KEY,
    application_id UUID NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
    team_id VARCHAR(128) NOT NULL,
    environment VARCHAR(32) NOT NULL DEFAULT 'preview',
    resource_type VARCHAR(64) NOT NULL,
    spec JSONB NOT NULL DEFAULT '{}'::jsonb,
    status VARCHAR(32) NOT NULL DEFAULT 'pending_approval',
    status_reason TEXT NOT NULL DEFAULT '',
    provider VARCHAR(64) NOT NULL DEFAULT 'unconfigured',
    outputs JSONB NOT NULL DEFAULT '{}'::jsonb,
    requested_by VARCHAR(128) NOT NULL,
    approved_by VARCHAR(128),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_resource_requests_app ON resource_requests (application_id);
CREATE INDEX IF NOT EXISTS idx_resource_requests_team ON resource_requests (team_id);
CREATE INDEX IF NOT EXISTS idx_resource_requests_status ON resource_requests (status);
CREATE INDEX IF NOT EXISTS idx_resource_requests_cursor ON resource_requests (created_at DESC, id DESC);
