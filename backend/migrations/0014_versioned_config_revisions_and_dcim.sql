-- 0014_versioned_config_revisions_and_dcim.sql
-- Immutable versioned environment configuration, compare-and-set pointers,
-- run/deployment configuration pinning, and server health tracking.

CREATE TABLE IF NOT EXISTS module_config_revisions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    module_id VARCHAR(63) NOT NULL REFERENCES modules(id) ON DELETE CASCADE,
    revision_number INT NOT NULL,
    pipeline_config JSONB NOT NULL DEFAULT '{}'::jsonb,
    deployment_config JSONB NOT NULL DEFAULT '[]'::jsonb,
    change_summary TEXT NOT NULL DEFAULT '',
    status VARCHAR(32) NOT NULL DEFAULT 'active'
        CHECK (status IN ('draft', 'pending_approval', 'active', 'superseded', 'rejected')),
    created_by VARCHAR(255) NOT NULL,
    approved_by VARCHAR(255),
    approved_at TIMESTAMPTZ,
    rejection_reason TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (module_id, revision_number)
);

CREATE INDEX IF NOT EXISTS idx_module_config_revisions_module ON module_config_revisions(module_id);

ALTER TABLE modules ADD COLUMN IF NOT EXISTS active_config_revision_id UUID REFERENCES module_config_revisions(id);
ALTER TABLE modules ADD COLUMN IF NOT EXISTS config_version INT NOT NULL DEFAULT 1;

ALTER TABLE pipeline_runs ADD COLUMN IF NOT EXISTS config_revision_id UUID REFERENCES module_config_revisions(id);
CREATE INDEX IF NOT EXISTS idx_pipeline_runs_config_rev ON pipeline_runs(config_revision_id);

ALTER TABLE deployments ADD COLUMN IF NOT EXISTS config_revision_id UUID REFERENCES module_config_revisions(id);
CREATE INDEX IF NOT EXISTS idx_deployments_config_rev ON deployments(config_revision_id);

CREATE TABLE IF NOT EXISTS server_health_records (
    server_name VARCHAR(255) PRIMARY KEY,
    status VARCHAR(32) NOT NULL,
    source VARCHAR(64) NOT NULL,
    freshness_seconds INT NOT NULL DEFAULT 0,
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    observed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
