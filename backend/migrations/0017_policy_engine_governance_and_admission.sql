-- 0017_policy_engine_governance_and_admission.sql
-- Phase 11 (P2.2): Enterprise Governance, Policy Decisions, Security Exceptions, Break-Glass, Quotas, and Admission Control

-- 1. Policy Decisions: Durable record of all policy evaluations across artifact admission, production approval, and deployment gates
CREATE TABLE IF NOT EXISTS policy_decisions (
    id UUID PRIMARY KEY,
    scope VARCHAR(64) NOT NULL,
    target_type VARCHAR(64) NOT NULL,
    target_id VARCHAR(128) NOT NULL,
    allowed BOOLEAN NOT NULL,
    reason TEXT NOT NULL,
    risk_score INTEGER NOT NULL DEFAULT 0,
    checks JSONB NOT NULL DEFAULT '{}'::jsonb,
    rules_evaluated TEXT[] NOT NULL DEFAULT '{}',
    evaluator VARCHAR(64) NOT NULL DEFAULT 'builtin',
    evaluated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_policy_decisions_target ON policy_decisions (target_type, target_id);
CREATE INDEX IF NOT EXISTS idx_policy_decisions_cursor ON policy_decisions (evaluated_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_policy_decisions_scope_allowed ON policy_decisions (scope, allowed);

-- 2. Security Exceptions: Time-boxed, CVE-bound, artifact-digest-pinned vulnerability waivers with dual control
CREATE TABLE IF NOT EXISTS security_exceptions (
    id UUID PRIMARY KEY,
    cve VARCHAR(64) NOT NULL,
    artifact_digest VARCHAR(128) NOT NULL,
    owner VARCHAR(128) NOT NULL,
    reason TEXT NOT NULL,
    approved_by VARCHAR(128) NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'active',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL,
    revoked_at TIMESTAMPTZ,
    revoked_by VARCHAR(128)
);

CREATE INDEX IF NOT EXISTS idx_security_exceptions_lookup ON security_exceptions (cve, artifact_digest, status);
CREATE INDEX IF NOT EXISTS idx_security_exceptions_expiry ON security_exceptions (expires_at) WHERE status = 'active';

-- 3. Break-Glass Requests: Two-person emergency bypass mechanism with short-lived leases
CREATE TABLE IF NOT EXISTS break_glass_requests (
    id UUID PRIMARY KEY,
    target_type VARCHAR(64) NOT NULL,
    target_id VARCHAR(128) NOT NULL,
    requested_by VARCHAR(128) NOT NULL,
    reason TEXT NOT NULL,
    incident_ticket VARCHAR(64) NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'pending',
    approved_by VARCHAR(128),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    approved_at TIMESTAMPTZ,
    expires_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_break_glass_target ON break_glass_requests (target_type, target_id, status);
CREATE INDEX IF NOT EXISTS idx_break_glass_active ON break_glass_requests (status, expires_at) WHERE status = 'active';

-- 4. Resource Quotas: Concurrency limits per team, application, or global scope
CREATE TABLE IF NOT EXISTS resource_quotas (
    id UUID PRIMARY KEY,
    scope VARCHAR(64) NOT NULL,
    scope_id VARCHAR(128) NOT NULL,
    max_concurrent_pipelines INTEGER NOT NULL DEFAULT 5,
    max_concurrent_deployments INTEGER NOT NULL DEFAULT 2,
    max_production_requests_per_day INTEGER NOT NULL DEFAULT 20,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_resource_quotas_scope UNIQUE (scope, scope_id)
);

CREATE INDEX IF NOT EXISTS idx_resource_quotas_lookup ON resource_quotas (scope, scope_id);
