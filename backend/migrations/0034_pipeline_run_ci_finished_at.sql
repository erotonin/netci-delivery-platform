-- When a run left CI: its first move out of queued/running, to a terminal state or on to
-- approval. With admitted_at it measures how long the run held CI capacity, which the sum
-- of stage durations does not: it misses the agent pod starting and a checkout reported
-- without timings (on the lab, 17 s of stages in a 46 s build). Not backfilled: for runs
-- written before this, nobody recorded it, and inventing it from updated_at would make a
-- later deployment's timestamp look like a build's.
ALTER TABLE pipeline_runs ADD COLUMN IF NOT EXISTS ci_finished_at TIMESTAMPTZ;
