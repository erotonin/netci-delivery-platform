-- 0019_security_waivers_and_telemetry.sql
-- Enterprise upgrades: VEX security waivers, server maintenance states, and L7 canary rules.

CREATE TABLE IF NOT EXISTS security_waivers (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    cve_id VARCHAR(64) NOT NULL,
    module_id VARCHAR(64),
    reason TEXT NOT NULL,
    approved_by VARCHAR(255) NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'expired', 'revoked')),
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_security_waivers_lookup
    ON security_waivers (cve_id, status, expires_at);

CREATE TABLE IF NOT EXISTS server_maintenance_states (
    server_name VARCHAR(255) PRIMARY KEY,
    in_maintenance BOOLEAN NOT NULL DEFAULT TRUE,
    reason TEXT NOT NULL DEFAULT '',
    updated_by VARCHAR(255) NOT NULL DEFAULT 'operator',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE production_requests
    ADD COLUMN IF NOT EXISTS canary_rules JSONB NOT NULL DEFAULT '{}'::jsonb;
