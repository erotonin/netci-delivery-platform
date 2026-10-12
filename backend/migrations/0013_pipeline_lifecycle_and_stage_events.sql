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
