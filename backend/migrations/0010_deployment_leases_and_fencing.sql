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
