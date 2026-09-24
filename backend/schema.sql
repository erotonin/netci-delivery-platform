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

-- >>> migration: 0014_versioned_config_revisions_and_dcim.sql
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

-- >>> migration: 0015_observability_and_notifications.sql
-- Migration 0015: Observability, Transactional Notifications Outbox, and Pagination Indexes

CREATE TABLE IF NOT EXISTS notifications (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    event_type VARCHAR(64) NOT NULL,
    aggregate_type VARCHAR(32) NOT NULL,
    aggregate_id VARCHAR(64) NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    recipient VARCHAR(255) NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'delivered', 'failed', 'dead_letter')),
    attempt INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 5,
    last_attempt_at TIMESTAMPTZ,
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    delivered_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_notifications_outbox
    ON notifications (status, next_attempt_at)
    WHERE status IN ('pending', 'failed');

CREATE INDEX IF NOT EXISTS idx_notifications_aggregate
    ON notifications (aggregate_type, aggregate_id);

CREATE INDEX IF NOT EXISTS idx_notifications_cursor
    ON notifications (created_at DESC, id DESC);

CREATE INDEX IF NOT EXISTS idx_pipeline_runs_cursor
    ON pipeline_runs (created_at DESC, id DESC);

CREATE INDEX IF NOT EXISTS idx_deployments_cursor
    ON deployments (created_at DESC, id DESC);

CREATE INDEX IF NOT EXISTS idx_audit_events_cursor
    ON audit_events (occurred_at DESC, id DESC);

CREATE INDEX IF NOT EXISTS idx_callback_token_uses_expires
    ON callback_token_uses (expires_at);

-- >>> migration: 0016_multi_module_dag_and_progressive_delivery.sql
-- 0016_multi_module_dag_and_progressive_delivery.sql
-- Phase 10 (P2.1): Multi-Module DAG Release Plan, SAGA Orchestration, and Progressive Delivery

-- Enhance production_requests with release plan DAG and deployment strategy
ALTER TABLE production_requests
    ADD COLUMN IF NOT EXISTS release_plan JSONB,
    ADD COLUMN IF NOT EXISTS strategy VARCHAR(32) NOT NULL DEFAULT 'rolling',
    ADD COLUMN IF NOT EXISTS strategy_config JSONB NOT NULL DEFAULT '{}'::jsonb;

-- Enhance production_request_modules with dependencies, execution state, and deployment reference
ALTER TABLE production_request_modules
    ADD COLUMN IF NOT EXISTS dependencies TEXT[] NOT NULL DEFAULT '{}',
    ADD COLUMN IF NOT EXISTS status VARCHAR(32) NOT NULL DEFAULT 'pending',
    ADD COLUMN IF NOT EXISTS deployment_id UUID REFERENCES deployments(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS started_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS completed_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS error_message TEXT;

-- Enhance deployments with progressive delivery attributes (traffic weighting and blue-green color)
ALTER TABLE deployments
    ADD COLUMN IF NOT EXISTS strategy VARCHAR(32) NOT NULL DEFAULT 'rolling',
    ADD COLUMN IF NOT EXISTS traffic_weight INTEGER NOT NULL DEFAULT 100,
    ADD COLUMN IF NOT EXISTS active_color VARCHAR(16),
    ADD COLUMN IF NOT EXISTS canary_step INTEGER NOT NULL DEFAULT 0;

-- Indexes for efficient lookups by coordinator and status
CREATE INDEX IF NOT EXISTS idx_prm_request_status ON production_request_modules (request_id, status);
CREATE INDEX IF NOT EXISTS idx_prm_deployment_id ON production_request_modules (deployment_id) WHERE deployment_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_deployments_strategy ON deployments (strategy);

-- >>> migration: 0017_policy_engine_governance_and_admission.sql
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

-- >>> migration: 0018_service_catalog_and_self_service.sql
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

-- >>> migration: 0019_security_waivers_and_telemetry.sql
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

-- >>> migration: 0020_server_telemetry.sql
-- 0020_server_telemetry.sql
-- Telemetry reported by edge agents decides the pre-flight deployment gate, so it is
-- durable state and lives here, not in a process-local dict that a second API replica
-- cannot see and a restart forgets. One row per server: the latest observation.

CREATE TABLE IF NOT EXISTS server_telemetry (
    server_name VARCHAR(255) PRIMARY KEY,
    cpu_percent DOUBLE PRECISION NOT NULL,
    mem_percent DOUBLE PRECISION NOT NULL,
    disk_percent DOUBLE PRECISION NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL
);

-- >>> migration: 0021_stage_catalog.sql
-- 0021_stage_catalog.sql
-- The stage catalog as data. Built-in stages are the ones the shared pipeline implements;
-- custom stages are registered by a platform administrator and run a script that lives
-- in the application's repository (reviewed in git), anchored after a built-in stage.
-- Modules choose from this catalog through the portal; nobody edits a Jenkinsfile.

CREATE TABLE IF NOT EXISTS stage_catalog (
    id VARCHAR(64) PRIMARY KEY,
    name VARCHAR(120) NOT NULL,
    category VARCHAR(32) NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    kind VARCHAR(16) NOT NULL CHECK (kind IN ('builtin', 'custom')),
    -- Custom stages only: a repository-relative script path and the built-in stage
    -- after which it runs.
    script VARCHAR(255),
    after_stage VARCHAR(64) REFERENCES stage_catalog (id),
    -- A required stage cannot be removed from a module's pipeline: checkout, build,
    -- the supply-chain evidence stages and publish are what make an artifact deployable.
    required BOOLEAN NOT NULL DEFAULT FALSE,
    enabled_by_default BOOLEAN NOT NULL DEFAULT TRUE,
    position INTEGER NOT NULL,
    created_by VARCHAR(255) NOT NULL DEFAULT 'netci',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK ((kind = 'builtin' AND script IS NULL AND after_stage IS NULL)
        OR (kind = 'custom' AND script IS NOT NULL AND after_stage IS NOT NULL))
);

INSERT INTO stage_catalog (id, name, category, description, kind, required, enabled_by_default, position) VALUES
    ('checkout',           'Checkout source',      'source',   'Clone the commit netCI named; nothing else is built.',                 'builtin', TRUE,  TRUE, 10),
    ('unit-test',          'Unit tests',           'test',     'Run the template''s test script in the builder.',                      'builtin', FALSE, TRUE, 20),
    ('build',              'Build artifact/image', 'build',    'Build the image or binary from the checked-out source.',               'builtin', TRUE,  TRUE, 30),
    ('sbom',               'Generate SBOM',        'security', 'Syft SBOM of the artifact; netCI''s policy requires it.',              'builtin', TRUE,  TRUE, 40),
    ('vulnerability-scan', 'Vulnerability scan',   'security', 'Trivy scan; critical and high findings deny the artifact.',            'builtin', TRUE,  TRUE, 50),
    ('sign',               'Sign artifact',        'publish',  'Push to the registry and sign the digest with cosign.',                'builtin', TRUE,  TRUE, 60),
    ('publish',            'Publish artifact',     'publish',  'Check the evidence agrees with the artifact and submit it to netCI.',  'builtin', TRUE,  TRUE, 70),
    ('deploy',             'Deploy through netCI', 'deploy',   'netCI starts the deployment workflow once the artifact is admitted.',  'builtin', FALSE, TRUE, 80),
    ('health-check',       'Health check',         'verify',   'The runtime playbook''s health gate; a release that fails it is rolled back.', 'builtin', FALSE, TRUE, 90)
ON CONFLICT (id) DO NOTHING;

-- >>> migration: 0022_stage_catalog_governance_and_parameters.sql
-- 0022_stage_catalog_governance_and_parameters.sql
-- A custom stage is code that runs on every build agent of every module that selects
-- it. Registering one therefore takes two platform administrators: the one who proposes
-- it and a different one who approves it (the same separation of duties as production
-- approvals). Custom stages may also declare parameters a module sets per pipeline.

ALTER TABLE stage_catalog
    ADD COLUMN IF NOT EXISTS status VARCHAR(16) NOT NULL DEFAULT 'active'
        CHECK (status IN ('proposed', 'active', 'rejected')),
    ADD COLUMN IF NOT EXISTS approved_by VARCHAR(255),
    ADD COLUMN IF NOT EXISTS parameters JSONB NOT NULL DEFAULT '[]'::jsonb;

-- Values a module set for its custom stages: {"<stage id>": {"<NAME>": "<value>"}}.
ALTER TABLE applications
    ADD COLUMN IF NOT EXISTS stage_parameters JSONB NOT NULL DEFAULT '{}'::jsonb;

-- >>> migration: 0023_agent_fleet_and_replica_coordination.sql
-- 0023_agent_fleet_and_replica_coordination.sql
-- Edge agents connect to *one* API replica over a websocket. Which replica holds which
-- agent, and which diagnostic command is waiting for which agent, used to live in that
-- replica's memory: a second replica saw no agents and could dispatch nothing, and a
-- restart forgot every connection. Both facts are durable state now, so any replica can
-- answer "who is connected" and accept a command for an agent held elsewhere (ADR-032).

CREATE TABLE IF NOT EXISTS agent_connections (
    hostname      VARCHAR(253) PRIMARY KEY,
    agent_id      VARCHAR(128) NOT NULL,
    replica_id    VARCHAR(128) NOT NULL,
    token_jti     VARCHAR(64)  NOT NULL,
    connected_at  TIMESTAMPTZ  NOT NULL,
    last_seen_at  TIMESTAMPTZ  NOT NULL
);

CREATE INDEX IF NOT EXISTS agent_connections_replica_idx ON agent_connections (replica_id);

-- One row per requested command. `pending` until the replica holding the agent claims it
-- (`sent`), then `completed`/`failed` with the agent's answer, or `expired` when nothing
-- claimed or answered it before `expires_at`. The requesting replica polls the row.
CREATE TABLE IF NOT EXISTS agent_commands (
    id            UUID PRIMARY KEY,
    hostname      VARCHAR(253) NOT NULL,
    command       TEXT         NOT NULL,
    requested_by  VARCHAR(255) NOT NULL,
    status        VARCHAR(16)  NOT NULL CHECK (status IN ('pending', 'sent', 'completed', 'failed', 'expired')),
    result        JSONB,
    created_at    TIMESTAMPTZ  NOT NULL,
    expires_at    TIMESTAMPTZ  NOT NULL,
    claimed_by    VARCHAR(128),
    claimed_at    TIMESTAMPTZ,
    completed_at  TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS agent_commands_pending_idx ON agent_commands (hostname, status) WHERE status = 'pending';

-- >>> migration: 0024_production_request_cancelled.sql
-- 0024_production_request_cancelled.sql
-- A production request whose deployment was cancelled (by the requester or an
-- operator, before the release ran) is neither rejected nor blocked: nobody refused it
-- and nothing failed. It is `cancelled`, and says so.
ALTER TABLE production_requests DROP CONSTRAINT IF EXISTS production_requests_status_check;
ALTER TABLE production_requests ADD CONSTRAINT production_requests_status_check
    CHECK (status IN ('waiting_approval', 'approved', 'rejected', 'blocked', 'succeeded', 'cancelled'));

-- >>> migration: 0025_supersede_replaced_config_revisions.sql
-- Activation now marks the revision it replaces as `superseded`. Rows written before
-- that fix are still `active` although the module points elsewhere; the browser showed
-- every one of them as "Active". One-off repair; the invariant is enforced in code.
UPDATE module_config_revisions r
   SET status = 'superseded'
  FROM modules m
 WHERE r.module_id = m.id
   AND r.status = 'active'
   AND m.active_config_revision_id IS NOT NULL
   AND r.id <> m.active_config_revision_id;

-- >>> migration: 0026_index_pipeline_runs_by_artifact_digest.sql
-- The Kubernetes admission controller answers "may this image run?" by finding the
-- pipeline run that produced a digest. It did that by loading every pipeline run into
-- the application and scanning the list -- once per container, on an endpoint the
-- cluster calls for every pod. pipeline_runs is the table that grows fastest here, so
-- admission latency grew with it, and an admission webhook that exceeds its timeout
-- either blocks the pod or, with failurePolicy=Ignore, waves it through unchecked.
CREATE INDEX IF NOT EXISTS idx_pipeline_runs_artifact_digest
    ON pipeline_runs (artifact_digest)
    WHERE artifact_digest IS NOT NULL;

-- >>> migration: 0027_index_settled_deployments_per_environment.sql
-- "What is this environment running right now?" is asked on every deployment netCI
-- creates -- a CI result, a redeploy, a production promotion, a rollback -- and it was
-- answered by loading every deployment the application has ever had and picking the
-- newest settled one in Python. Nothing thins `deployments`: retention covers console
-- lines, delivery events, notifications and spent callback tokens, not the durable
-- record. So each release of a long-lived service read every release that came before it.
CREATE INDEX IF NOT EXISTS idx_deployments_settled_per_environment
    ON deployments (application_id, environment, updated_at DESC)
    WHERE status IN ('healthy', 'rolled_back');

-- >>> migration: 0028_pipeline_run_delivery_intent.sql
-- CI and CD become separate decisions (ADR-043). Until now every run built and then
-- deployed, so a run needed no field saying whether it would. These are fixed when the
-- run is queued: whether a successful build is deployed, whether it is published at all
-- (a fork's pull request is not: nothing unreviewed is signed), the tag a success
-- registers as a version, and what started it. Existing rows keep their meaning --
-- every one of them was a build that deployed.
ALTER TABLE pipeline_runs ADD COLUMN IF NOT EXISTS deploy_after_build BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE pipeline_runs ADD COLUMN IF NOT EXISTS publish_artifact BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE pipeline_runs ADD COLUMN IF NOT EXISTS release_tag TEXT;
ALTER TABLE pipeline_runs ADD COLUMN IF NOT EXISTS trigger JSONB NOT NULL DEFAULT '{}'::jsonb;
-- A run that is not published cannot carry a digest; the database refuses one, so no
-- code path can later mistake a fork's build for an artifact.
ALTER TABLE pipeline_runs DROP CONSTRAINT IF EXISTS pipeline_runs_unpublished_has_no_digest;
ALTER TABLE pipeline_runs ADD CONSTRAINT pipeline_runs_unpublished_has_no_digest
    CHECK (publish_artifact OR artifact_digest IS NULL);

-- >>> migration: 0029_deployment_healthy_at.sql
-- When a deployment became healthy, written once by the transition that made it so
-- (ADR-043). A promotion that requires "N minutes healthy in staging" needs this fact,
-- and nothing else records it: `updated_at` moves on every later transition, and the
-- delivery events DORA reads are written for production only. Rows from before this
-- column have no value, so they prove no soak -- a promotion that needs one refuses
-- rather than guessing from `updated_at`.
ALTER TABLE deployments ADD COLUMN IF NOT EXISTS healthy_at TIMESTAMPTZ;

-- >>> migration: 0030_artifact_sboms_and_findings.sql
-- What is inside each artifact, and which known vulnerabilities it carries (ADR-045).
-- netCI used to keep an SBOM only as a *path on the build agent*, which the agent took
-- with it when it was destroyed, so "where does CVE-X run?" had no answer once a CVE was
-- published after the build. The SBOM is now kept, keyed by the immutable digest, and
-- netCI re-scans it itself.
CREATE TABLE IF NOT EXISTS artifact_sboms (
    artifact_digest TEXT PRIMARY KEY CHECK (artifact_digest ~ '^sha256:[0-9a-f]{64}$'),
    application_id UUID NOT NULL REFERENCES applications(id),
    pipeline_run_id UUID NOT NULL REFERENCES pipeline_runs(id),
    format TEXT NOT NULL,
    document JSONB NOT NULL,
    component_count INTEGER NOT NULL CHECK (component_count >= 0),
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- One row per (digest, where it was found, vulnerability, package, version). `ci` rows
-- are the build's own scan; `rescan` rows are netCI's later scans of the stored SBOM,
-- replaced as a set each time so a fixed database entry disappears.
CREATE TABLE IF NOT EXISTS artifact_findings (
    artifact_digest TEXT NOT NULL,
    source TEXT NOT NULL CHECK (source IN ('ci', 'rescan')),
    vulnerability_id TEXT NOT NULL,
    package TEXT NOT NULL DEFAULT '',
    installed_version TEXT NOT NULL DEFAULT '',
    severity TEXT NOT NULL,
    fixed_version TEXT NOT NULL DEFAULT '',
    first_seen_at TIMESTAMPTZ NOT NULL,
    last_seen_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (artifact_digest, source, vulnerability_id, package, installed_version)
);
CREATE INDEX IF NOT EXISTS idx_artifact_findings_vulnerability
    ON artifact_findings (vulnerability_id);

-- When netCI last scanned a digest's SBOM, and whether that scan ran at all. A failed
-- scan is recorded as failed: an exposure answer must be able to say "not covered"
-- rather than read an absent scan as a clean one.
CREATE TABLE IF NOT EXISTS artifact_rescans (
    artifact_digest TEXT PRIMARY KEY,
    scanned_at TIMESTAMPTZ NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('scanned', 'failed')),
    scanner TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT ''
);
