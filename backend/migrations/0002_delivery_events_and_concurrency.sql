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
