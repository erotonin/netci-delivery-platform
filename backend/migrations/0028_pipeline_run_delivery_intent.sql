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
