-- 0012_scm_integrations_and_webhooks.sql
-- SCM provider integration, atomic webhook deduplication, and console URL persistence.

ALTER TABLE pipeline_runs ADD COLUMN IF NOT EXISTS console_url TEXT;

CREATE TABLE IF NOT EXISTS scm_integrations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    application_id UUID NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
    provider VARCHAR(32) NOT NULL,
    repository_identity VARCHAR(255) NOT NULL,
    secret_token TEXT,
    secret_token_hash VARCHAR(128),
    credential_reference VARCHAR(128),
    enabled BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (application_id, provider),
    UNIQUE (provider, repository_identity)
);

ALTER TABLE scm_integrations ADD COLUMN IF NOT EXISTS secret_token TEXT;

CREATE TABLE IF NOT EXISTS scm_webhook_deliveries (
    delivery_id VARCHAR(128) PRIMARY KEY,
    provider VARCHAR(32) NOT NULL,
    event_type VARCHAR(64) NOT NULL,
    repository_identity VARCHAR(255) NOT NULL,
    application_id UUID REFERENCES applications(id) ON DELETE CASCADE,
    commit_sha VARCHAR(128),
    status VARCHAR(32) NOT NULL,
    received_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS scm_webhook_deliveries_repo_idx ON scm_webhook_deliveries (repository_identity);
