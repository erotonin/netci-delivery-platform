-- When a deployment became healthy, written once by the transition that made it so
-- (ADR-043). A promotion that requires "N minutes healthy in staging" needs this fact,
-- and nothing else records it: `updated_at` moves on every later transition, and the
-- delivery events DORA reads are written for production only. Rows from before this
-- column have no value, so they prove no soak -- a promotion that needs one refuses
-- rather than guessing from `updated_at`.
ALTER TABLE deployments ADD COLUMN IF NOT EXISTS healthy_at TIMESTAMPTZ;
