-- Builds are admitted and superseded, not refused (ADR-050). A run is written queued as
-- before; `admitted_at` is when it was allowed to take CI capacity and was dispatched.
-- Until then it waits here, in PostgreSQL, instead of being answered 429.
ALTER TABLE pipeline_runs ADD COLUMN IF NOT EXISTS admitted_at TIMESTAMPTZ;
-- `<application>:<ref>` for SCM-triggered runs of one branch or pull request; NULL for tag,
-- manual and retried runs, which supersede nothing and are superseded by nothing.
ALTER TABLE pipeline_runs ADD COLUMN IF NOT EXISTS concurrency_group TEXT;
-- The newer run of the same group that cancelled this one.
ALTER TABLE pipeline_runs ADD COLUMN IF NOT EXISTS superseded_by UUID REFERENCES pipeline_runs(id);

-- Every run written before this was dispatched as it was written: it was admitted then.
-- Left NULL, a still-queued run of that time would be dispatched a second time.
UPDATE pipeline_runs SET admitted_at = created_at WHERE admitted_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_pipeline_runs_awaiting_admission
    ON pipeline_runs (created_at, id) WHERE admitted_at IS NULL AND status = 'queued';
CREATE INDEX IF NOT EXISTS idx_pipeline_runs_concurrency_group
    ON pipeline_runs (concurrency_group, created_at)
    WHERE concurrency_group IS NOT NULL AND status IN ('queued', 'running');

-- The one refusal left: a queue that could grow without bound only moves the overload
-- from Jenkins to the database.
ALTER TABLE resource_quotas ADD COLUMN IF NOT EXISTS max_queued_pipelines INTEGER NOT NULL DEFAULT 50;
ALTER TABLE resource_quotas DROP CONSTRAINT IF EXISTS resource_quotas_max_queued_positive;
ALTER TABLE resource_quotas ADD CONSTRAINT resource_quotas_max_queued_positive CHECK (max_queued_pipelines > 0);
