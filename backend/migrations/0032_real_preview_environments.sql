-- Preview environments become real deployments (ADR-049). The table existed, but a row
-- was written as "active" with a URL nobody served: nothing was ever deployed. A preview
-- now belongs to the pull-request run that built it, is "deploying" until the worker
-- reports, and records what the worker said. Rows written before this carry no run.
ALTER TABLE preview_environments ADD COLUMN IF NOT EXISTS pipeline_run_id UUID REFERENCES pipeline_runs(id);
ALTER TABLE preview_environments ADD COLUMN IF NOT EXISTS artifact_digest TEXT;
ALTER TABLE preview_environments ADD COLUMN IF NOT EXISTS release_name TEXT;
ALTER TABLE preview_environments ADD COLUMN IF NOT EXISTS detail TEXT NOT NULL DEFAULT '';
-- A URL is known only once something serves it.
ALTER TABLE preview_environments ALTER COLUMN url DROP NOT NULL;
-- The fake "active" rows: nothing was deployed for them, so they are not active.
UPDATE preview_environments SET status = 'unverified', detail = 'recorded before previews were deployed; nothing was ever served'
 WHERE pipeline_run_id IS NULL AND status IN ('pending', 'active');
ALTER TABLE preview_environments DROP CONSTRAINT IF EXISTS preview_environments_status_known;
ALTER TABLE preview_environments ADD CONSTRAINT preview_environments_status_known
    CHECK (status IN ('deploying', 'active', 'failed', 'destroying', 'destroyed', 'expired', 'unverified'));
