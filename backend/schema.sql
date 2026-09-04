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

-- >>> migration: 0004_application_ownership.sql
-- Which team owns an application.
--
-- Roles alone are global: before this, any developer could run any team's pipeline and any
-- reviewer could approve any team's production release. That is workable for one team and
-- wrong for an organisation, where "who may deploy this" is a property of the application,
-- not only of the person.
--
-- Existing rows have no owner, and are left that way rather than being assigned to a team
-- nobody chose. An unowned application keeps behaving exactly as it does today -- role
-- checks apply, team checks do not -- so this migration changes no existing behaviour.
-- Set NETCI_REQUIRE_APPLICATION_OWNER=true once every application has an owner, and an
-- unowned one then becomes platform-admin only. That is the migration path: adopt
-- gradually, then close the door.
ALTER TABLE applications ADD COLUMN IF NOT EXISTS owner_team VARCHAR(255);

COMMENT ON COLUMN applications.owner_team IS
    'Team that owns this application. NULL means unowned: role checks still apply, team checks do not.';

-- Listing "my team''s applications" is the common Portal query once ownership is in use.
CREATE INDEX IF NOT EXISTS applications_owner_team_idx ON applications (owner_team);

-- >>> migration: 0005_security_evidence.sql
-- Durable supply-chain evidence.
--
-- Deployment policy is evaluated again after process restarts, so retaining only the
-- allow/deny audit decision is insufficient: the signed digest, SBOM and vulnerability
-- report that produced the decision must remain available as one authoritative record.
CREATE TABLE IF NOT EXISTS security_evidence (
    pipeline_run_id UUID PRIMARY KEY REFERENCES pipeline_runs(id) ON DELETE CASCADE,
    application_id UUID NOT NULL REFERENCES applications(id),
    artifact_digest TEXT NOT NULL CHECK (artifact_digest ~ '^sha256:[0-9a-f]{64}$'),
    evidence JSONB NOT NULL,
    decision VARCHAR(16) NOT NULL CHECK (decision IN ('allow', 'deny')),
    reason TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS security_evidence_application_idx
    ON security_evidence (application_id, updated_at DESC);

-- >>> migration: 0006_production_request_completion.sql
-- Production requests track the terminal result of the deployment they created.
ALTER TABLE production_requests DROP CONSTRAINT IF EXISTS production_requests_status_check;
ALTER TABLE production_requests ADD CONSTRAINT production_requests_status_check
    CHECK (status IN ('waiting_approval', 'approved', 'rejected', 'blocked', 'succeeded'));

-- >>> migration: 0007_production_request_idempotency.sql
-- Portal command idempotency must survive API restarts.
ALTER TABLE production_requests ADD COLUMN IF NOT EXISTS idempotency_key VARCHAR(128);
ALTER TABLE production_requests ADD COLUMN IF NOT EXISTS request_hash CHAR(64);
CREATE UNIQUE INDEX IF NOT EXISTS production_requests_idempotency_idx
    ON production_requests (idempotency_key) WHERE idempotency_key IS NOT NULL;

-- >>> migration: 0008_system_unknown_status.sql
-- A newly registered system has no health evidence yet and must not start green.
ALTER TABLE systems DROP CONSTRAINT IF EXISTS systems_status_check;
ALTER TABLE systems ALTER COLUMN status SET DEFAULT 'unknown';
ALTER TABLE systems ADD CONSTRAINT systems_status_check
    CHECK (status IN ('unknown', 'healthy', 'degraded', 'critical'));

-- >>> migration: 0009_callback_token_use.sql
-- Record every callback token that is actually used.
--
-- Two things need this. A token whose scope is terminal -- reporting a deployment
-- result -- must be usable once: a token scraped from a worker's environment could
-- otherwise be replayed later to overwrite a newer result with an older one. And a
-- security review needs to be able to answer "which workload wrote this?" from the
-- database rather than from a log that may have rotated away.
--
-- Only the `jti` is stored. The token, its signature and the signing key never reach
-- this table, so a database dump cannot be turned back into a working credential.
CREATE TABLE IF NOT EXISTS callback_token_uses (
    jti CHAR(32) PRIMARY KEY,
    workload VARCHAR(32) NOT NULL,
    application_id UUID NOT NULL REFERENCES applications(id),
    pipeline_run_id UUID REFERENCES pipeline_runs(id),
    deployment_id UUID REFERENCES deployments(id),
    operation VARCHAR(64) NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    used_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT callback_token_uses_names_one_resource
        CHECK ((pipeline_run_id IS NULL) <> (deployment_id IS NULL))
);

-- Expired rows are worthless once the token they describe can no longer be presented;
-- this index is what makes the periodic delete cheap.
CREATE INDEX IF NOT EXISTS callback_token_uses_expires_at_idx
    ON callback_token_uses (expires_at);

-- >>> migration: 0010_deployment_leases_and_fencing.sql
-- Deployment leases, fencing tokens and a safe pipeline-log sequence.
--
-- Three defects this closes.
--
-- 1. Nothing stopped two deployments running against the same target at once. Two
--    approvals seconds apart produced two workflows writing to the same hosts, and the
--    surviving state was whichever finished last. A partial unique index makes the
--    database refuse the second one: it is the only place that can decide, because the
--    two requests may be handled by different API replicas.
--
-- 2. A workflow that lost its lease -- timed out, was superseded, or came back from a
--    long pause -- could still report a result and overwrite the newer workflow's. A
--    monotonic fencing token makes a stale writer detectable: a callback carrying a token
--    lower than the row's current one is refused rather than applied.
--
-- 3. `INSERT INTO pipeline_logs ... (SELECT max(sequence) + 1 ...)` is not safe under
--    concurrency. Two transactions read the same max and both insert the same sequence;
--    one dies on the primary key and takes its whole unit of work with it. A per-run
--    counter, incremented with UPDATE ... RETURNING, hands out each number once.

CREATE TABLE IF NOT EXISTS deployment_leases (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    application_id UUID NOT NULL REFERENCES applications(id),
    environment VARCHAR(32) NOT NULL CHECK (environment IN ('dev', 'staging', 'prod')),
    -- The logical target within the environment: a namespace, a host set, a release name.
    -- Two deployments to the same application+environment+target collide; two to
    -- different targets do not.
    target VARCHAR(255) NOT NULL,
    deployment_id UUID NOT NULL REFERENCES deployments(id),
    -- Who holds it. A workflow id when Temporal owns the deployment, otherwise the API
    -- request that created it -- either way, a name a human can chase.
    owner VARCHAR(255) NOT NULL,
    -- Monotonic per target. Every callback must carry this, and a lower one is stale.
    fencing_token BIGINT NOT NULL,
    acquired_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    heartbeat_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL,
    released_at TIMESTAMPTZ,
    release_reason VARCHAR(64)
);

-- The invariant, enforced by the database rather than by a check-then-act in one replica:
-- at most one unreleased lease per (application, environment, target).
CREATE UNIQUE INDEX IF NOT EXISTS deployment_leases_one_active_per_target
    ON deployment_leases (application_id, environment, target)
    WHERE released_at IS NULL;

CREATE INDEX IF NOT EXISTS deployment_leases_deployment_idx
    ON deployment_leases (deployment_id);
CREATE INDEX IF NOT EXISTS deployment_leases_expiry_idx
    ON deployment_leases (expires_at) WHERE released_at IS NULL;

-- Fencing tokens must keep rising for a target even after every lease on it is released,
-- or a new lease could reissue a number a stale workflow still holds.
CREATE TABLE IF NOT EXISTS deployment_fencing_counters (
    application_id UUID NOT NULL REFERENCES applications(id),
    environment VARCHAR(32) NOT NULL,
    target VARCHAR(255) NOT NULL,
    next_token BIGINT NOT NULL DEFAULT 1,
    PRIMARY KEY (application_id, environment, target)
);

-- The deployment records the token it was fenced with, so a callback can be checked
-- without joining back to a lease that may already have been released.
ALTER TABLE deployments ADD COLUMN IF NOT EXISTS fencing_token BIGINT;

COMMENT ON COLUMN deployments.fencing_token IS
    'Lease generation this deployment was started under. A callback carrying a lower token is stale and is refused.';

-- Terminal states a deployment can genuinely reach. `rolled_back` already existed and is
-- kept; `restored` was considered and rejected as a synonym that would split queries.
ALTER TABLE deployments DROP CONSTRAINT IF EXISTS deployments_status_check;
ALTER TABLE deployments ADD CONSTRAINT deployments_status_check
    CHECK (status IN (
        'pending_approval',
        'deploying',
        'healthy',
        'failed',
        'rollback_in_progress',
        'rolled_back',
        'rollback_failed',
        'cancelled'
    ));

-- Per-run log sequence counter. Existing rows are backfilled from the log lines they
-- already have, so an upgraded installation continues where it left off instead of
-- colliding with its own history.
CREATE TABLE IF NOT EXISTS pipeline_log_sequences (
    pipeline_run_id UUID PRIMARY KEY REFERENCES pipeline_runs(id) ON DELETE CASCADE,
    next_sequence INTEGER NOT NULL DEFAULT 1
);

INSERT INTO pipeline_log_sequences (pipeline_run_id, next_sequence)
SELECT pipeline_run_id, max(sequence) + 1 FROM pipeline_logs GROUP BY pipeline_run_id
ON CONFLICT (pipeline_run_id) DO NOTHING;

-- Every lease acquisition, conflict, expiry, recovery and stale callback is an audit
-- event written through the existing audit_events table; no new table is needed, and
-- keeping them there means one query answers "what happened to this deployment".

-- >>> migration: 0011_release_immutability_and_ci_reports.sql
-- Release immutability and version CI report separation
--
-- Two defects this closes:
-- 1. `release_versions` previously used `ON CONFLICT DO UPDATE SET metadata = EXCLUDED.metadata`,
--    allowing an existing release's digest, provenance, or metadata to be mutated after publication.
-- 2. CI quality reports were merged directly into `release_versions.metadata`, coupling mutable
--    CI evidence with the immutable release version artifact definition.
--
-- This migration introduces `version_ci_reports` as an append-only ledger for CI quality reports
-- and ensures release versions remain strictly immutable.

CREATE TABLE IF NOT EXISTS version_ci_reports (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    module_id VARCHAR(63) NOT NULL REFERENCES modules(id) ON DELETE CASCADE,
    version VARCHAR(128) NOT NULL,
    report JSONB NOT NULL,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    recorded_by VARCHAR(255) NOT NULL DEFAULT 'netCI Pipeline'
);

CREATE INDEX IF NOT EXISTS idx_version_ci_reports_lookup
    ON version_ci_reports (module_id, version, recorded_at DESC);

-- >>> migration: 0012_scm_integrations_and_webhooks.sql
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

-- >>> migration: 0013_pipeline_lifecycle_and_stage_events.sql
-- 0013_pipeline_lifecycle_and_stage_events.sql
-- Pipeline retry lineage, stage execution event persistence, and reconciler tracking.

ALTER TABLE pipeline_runs ADD COLUMN IF NOT EXISTS retry_of UUID REFERENCES pipeline_runs(id);
CREATE INDEX IF NOT EXISTS idx_pipeline_runs_retry_of ON pipeline_runs(retry_of);

CREATE TABLE IF NOT EXISTS pipeline_stages (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    pipeline_run_id UUID NOT NULL REFERENCES pipeline_runs(id) ON DELETE CASCADE,
    stage_id VARCHAR(64) NOT NULL,
    stage_name VARCHAR(128) NOT NULL,
    attempt INT NOT NULL DEFAULT 1,
    status VARCHAR(32) NOT NULL,
    queued_at TIMESTAMPTZ,
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    duration_ms BIGINT,
    error_message TEXT,
    log_snippet TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (pipeline_run_id, stage_id, attempt)
);

CREATE INDEX IF NOT EXISTS idx_pipeline_stages_run ON pipeline_stages(pipeline_run_id);
