-- GENERATED FILE - do not edit.
-- Concatenation of backend/migrations/*.sql, produced by `python scripts/migrate.py --emit-schema`.
-- Used by the compose initdb mount; `make migrate` applies the same files to an existing database.

-- >>> migration: 0001_baseline.sql
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS applications (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name VARCHAR(63) NOT NULL UNIQUE,
    repository_url TEXT NOT NULL,
    pipeline_template VARCHAR(128) NOT NULL,
    runtime VARCHAR(32) NOT NULL CHECK (runtime IN ('docker', 'kubernetes', 'systemd')),
    default_environment VARCHAR(32) NOT NULL DEFAULT 'dev'
        CHECK (default_environment IN ('dev', 'staging', 'prod')),
    stages JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS pipeline_runs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    application_id UUID NOT NULL REFERENCES applications(id),
    commit_sha VARCHAR(128) NOT NULL,
    branch VARCHAR(255) NOT NULL DEFAULT 'main',
    environment VARCHAR(32) NOT NULL CHECK (environment IN ('dev', 'staging', 'prod')),
    parameters JSONB NOT NULL DEFAULT '{}'::jsonb,
    status VARCHAR(32) NOT NULL
        CHECK (status IN ('queued', 'running', 'waiting_approval', 'succeeded', 'failed', 'cancelled', 'rolled_back')),
    jenkins_run_id VARCHAR(255),
    workflow_id VARCHAR(255),
    artifact_digest TEXT,
    correlation_id VARCHAR(255),
    idempotency_key VARCHAR(128),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (application_id, idempotency_key)
);

ALTER TABLE pipeline_runs ADD COLUMN IF NOT EXISTS parameters JSONB NOT NULL DEFAULT '{}'::jsonb;

CREATE TABLE IF NOT EXISTS deployments (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    application_id UUID NOT NULL REFERENCES applications(id),
    pipeline_run_id UUID REFERENCES pipeline_runs(id),
    runtime VARCHAR(32) NOT NULL CHECK (runtime IN ('docker', 'kubernetes', 'systemd')),
    environment VARCHAR(32) NOT NULL CHECK (environment IN ('dev', 'staging', 'prod')),
    status VARCHAR(32) NOT NULL
        CHECK (status IN ('pending_approval', 'deploying', 'healthy', 'failed', 'rolled_back')),
    artifact_digest TEXT NOT NULL CHECK (artifact_digest ~ '^sha256:[0-9a-f]{64}$'),
    approved_by VARCHAR(255),
    previous_artifact_digest TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS audit_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    event_type VARCHAR(128) NOT NULL,
    application_id UUID REFERENCES applications(id),
    pipeline_run_id UUID REFERENCES pipeline_runs(id),
    deployment_id UUID REFERENCES deployments(id),
    actor VARCHAR(255),
    correlation_id VARCHAR(255),
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS idempotency_records (
    scope VARCHAR(128) NOT NULL,
    idempotency_key VARCHAR(128) NOT NULL,
    request_hash CHAR(64) NOT NULL,
    resource_type VARCHAR(64) NOT NULL,
    resource_id UUID NOT NULL,
    response_status INTEGER NOT NULL,
    response_body JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (scope, idempotency_key)
);

CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS pipeline_runs_set_updated_at ON pipeline_runs;
CREATE TRIGGER pipeline_runs_set_updated_at
BEFORE UPDATE ON pipeline_runs
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

DROP TRIGGER IF EXISTS deployments_set_updated_at ON deployments;
CREATE TRIGGER deployments_set_updated_at
BEFORE UPDATE ON deployments
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE INDEX IF NOT EXISTS idx_pipeline_runs_application ON pipeline_runs(application_id);
CREATE INDEX IF NOT EXISTS idx_deployments_application ON deployments(application_id);
CREATE INDEX IF NOT EXISTS idx_audit_events_occurred_at ON audit_events(occurred_at);
CREATE INDEX IF NOT EXISTS idx_idempotency_records_created_at ON idempotency_records(created_at);

-- Release Portal hierarchy and read-model metadata.
CREATE TABLE IF NOT EXISTS systems (
    id VARCHAR(63) PRIMARY KEY,
    unit TEXT NOT NULL,
    description TEXT NOT NULL,
    owner VARCHAR(255) NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'healthy'
        CHECK (status IN ('healthy', 'degraded', 'critical')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS modules (
    id VARCHAR(63) PRIMARY KEY,
    system_id VARCHAR(63) NOT NULL REFERENCES systems(id) ON DELETE CASCADE,
    application_id UUID UNIQUE REFERENCES applications(id),
    runtime VARCHAR(32) NOT NULL DEFAULT 'docker'
        CHECK (runtime IN ('docker', 'kubernetes', 'systemd')),
    name VARCHAR(255) NOT NULL,
    module_type VARCHAR(64) NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    deployment_config JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (system_id, name)
);

ALTER TABLE modules
    ADD COLUMN IF NOT EXISTS deployment_config JSONB NOT NULL DEFAULT '[]'::jsonb;

ALTER TABLE modules
    ADD COLUMN IF NOT EXISTS pipeline_config JSONB NOT NULL DEFAULT '{}'::jsonb;

ALTER TABLE modules ADD COLUMN IF NOT EXISTS runtime VARCHAR(32) NOT NULL DEFAULT 'docker';

CREATE TABLE IF NOT EXISTS release_versions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    module_id VARCHAR(63) NOT NULL REFERENCES modules(id) ON DELETE CASCADE,
    version VARCHAR(128) NOT NULL,
    artifact_digest TEXT,
    sbom_uri TEXT,
    signature_uri TEXT,
    signature_verified BOOLEAN NOT NULL DEFAULT false,
    vulnerability_status VARCHAR(32) NOT NULL DEFAULT 'not_available',
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (module_id, version)
);

ALTER TABLE release_versions ADD COLUMN IF NOT EXISTS metadata JSONB NOT NULL DEFAULT '{}'::jsonb;

CREATE TABLE IF NOT EXISTS production_requests (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    module_id VARCHAR(63) NOT NULL REFERENCES modules(id) ON DELETE CASCADE,
    release_version_id UUID REFERENCES release_versions(id),
    deployment_id UUID REFERENCES deployments(id),
    requested_by VARCHAR(255) NOT NULL,
    version VARCHAR(128) NOT NULL DEFAULT 'v0.0.0',
    scheduled_for TIMESTAMPTZ NOT NULL DEFAULT now(),
    rollback_strategy VARCHAR(16) NOT NULL DEFAULT 'automatic'
        CHECK (rollback_strategy IN ('automatic', 'manual')),
    run_automation_tests BOOLEAN NOT NULL DEFAULT true,
    status VARCHAR(32) NOT NULL DEFAULT 'waiting_approval'
        CHECK (status IN ('waiting_approval', 'approved', 'rejected', 'blocked')),
    comment TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE production_requests ADD COLUMN IF NOT EXISTS version VARCHAR(128) NOT NULL DEFAULT 'v0.0.0';
ALTER TABLE production_requests ADD COLUMN IF NOT EXISTS scheduled_for TIMESTAMPTZ NOT NULL DEFAULT now();
ALTER TABLE production_requests ADD COLUMN IF NOT EXISTS rollback_strategy VARCHAR(16) NOT NULL DEFAULT 'automatic';
ALTER TABLE production_requests ADD COLUMN IF NOT EXISTS run_automation_tests BOOLEAN NOT NULL DEFAULT true;

CREATE TABLE IF NOT EXISTS production_request_modules (
    request_id UUID NOT NULL REFERENCES production_requests(id) ON DELETE CASCADE,
    module_id VARCHAR(63) NOT NULL REFERENCES modules(id) ON DELETE RESTRICT,
    version VARCHAR(128) NOT NULL,
    deployment_order SMALLINT NOT NULL CHECK (deployment_order BETWEEN 1 AND 100),
    PRIMARY KEY (request_id, module_id)
);

CREATE INDEX IF NOT EXISTS idx_modules_system ON modules(system_id);
CREATE INDEX IF NOT EXISTS idx_release_versions_module ON release_versions(module_id);
CREATE INDEX IF NOT EXISTS idx_production_requests_status ON production_requests(status);
CREATE INDEX IF NOT EXISTS idx_production_requests_module ON production_requests(module_id);
CREATE INDEX IF NOT EXISTS idx_production_request_modules_order ON production_request_modules(request_id, deployment_order);

DROP TRIGGER IF EXISTS systems_set_updated_at ON systems;
CREATE TRIGGER systems_set_updated_at
BEFORE UPDATE ON systems
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

DROP TRIGGER IF EXISTS modules_set_updated_at ON modules;
CREATE TRIGGER modules_set_updated_at
BEFORE UPDATE ON modules
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

DROP TRIGGER IF EXISTS production_requests_set_updated_at ON production_requests;
CREATE TRIGGER production_requests_set_updated_at
BEFORE UPDATE ON production_requests
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- >>> migration: 0002_delivery_events_and_concurrency.sql
-- Source events for the DORA projection, durable pipeline logs and the
-- optimistic-concurrency columns that stop two callbacks from both winning.

ALTER TABLE pipeline_runs ADD COLUMN IF NOT EXISTS version INTEGER NOT NULL DEFAULT 1;
ALTER TABLE deployments ADD COLUMN IF NOT EXISTS version INTEGER NOT NULL DEFAULT 1;

CREATE TABLE IF NOT EXISTS delivery_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    event_type VARCHAR(32) NOT NULL CHECK (event_type IN ('commit', 'deployment', 'recovery')),
    application_id UUID NOT NULL REFERENCES applications(id),
    pipeline_run_id UUID REFERENCES pipeline_runs(id),
    deployment_id UUID REFERENCES deployments(id),
    commit_sha VARCHAR(128),
    environment VARCHAR(32) CHECK (environment IN ('dev', 'staging', 'prod')),
    successful BOOLEAN,
    requires_intervention BOOLEAN NOT NULL DEFAULT false,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_delivery_events_application ON delivery_events(application_id, occurred_at);
CREATE INDEX IF NOT EXISTS idx_delivery_events_type ON delivery_events(event_type, environment);

CREATE TABLE IF NOT EXISTS pipeline_logs (
    pipeline_run_id UUID NOT NULL REFERENCES pipeline_runs(id) ON DELETE CASCADE,
    sequence INTEGER NOT NULL,
    line TEXT NOT NULL,
    written_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (pipeline_run_id, sequence)
);

-- The trigger would overwrite the updated_at a compare-and-set write supplies,
-- which would make the persisted timestamp disagree with the returned record.
DROP TRIGGER IF EXISTS pipeline_runs_set_updated_at ON pipeline_runs;
DROP TRIGGER IF EXISTS deployments_set_updated_at ON deployments;

-- >>> migration: 0003_pipeline_run_actor.sql
-- Record who started a pipeline run.
--
-- Two things need this. The audit trail needs to name a person for every run, not only
-- for approvals. And separation of duties on a production deployment is a comparison
-- between the person who asked for the release and the person approving it -- without a
-- requester on the run, the approval endpoint has nothing to compare against and the
-- control silently degrades to "someone pressed the button twice".
--
-- Existing rows predate authentication and genuinely have no known actor, so they are
-- left NULL rather than backfilled with a name nobody chose. `require_separation_of_duties`
-- treats an unknown requester as "cannot be shown to be the same person" and allows the
-- approval; the alternative would be to lock out every deployment created before upgrade.
ALTER TABLE pipeline_runs ADD COLUMN IF NOT EXISTS started_by VARCHAR(255);

COMMENT ON COLUMN pipeline_runs.started_by IS
    'Verified subject of the principal that started the run; NULL for runs created before authentication existed.';
