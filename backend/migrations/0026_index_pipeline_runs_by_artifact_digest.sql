-- The Kubernetes admission controller answers "may this image run?" by finding the
-- pipeline run that produced a digest. It did that by loading every pipeline run into
-- the application and scanning the list -- once per container, on an endpoint the
-- cluster calls for every pod. pipeline_runs is the table that grows fastest here, so
-- admission latency grew with it, and an admission webhook that exceeds its timeout
-- either blocks the pod or, with failurePolicy=Ignore, waves it through unchecked.
CREATE INDEX IF NOT EXISTS idx_pipeline_runs_artifact_digest
    ON pipeline_runs (artifact_digest)
    WHERE artifact_digest IS NOT NULL;
