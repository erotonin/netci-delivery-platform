-- One build per commit for a branch and its merge request (ADR-095): an SCM event finds the run
-- of its commit in its concurrency group -- queued, running or already succeeded -- and joins it.
CREATE INDEX IF NOT EXISTS idx_pipeline_runs_group_commit
    ON pipeline_runs (concurrency_group, commit_sha)
    WHERE concurrency_group IS NOT NULL;
