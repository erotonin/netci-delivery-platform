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
