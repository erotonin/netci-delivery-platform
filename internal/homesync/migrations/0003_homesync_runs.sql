-- A netCI run (netci-queue's run id, carried on the build by the plugin's NetciRunAction) gets at
-- most one build number across every replica (ADR-081). The reservation made before a build takes
-- its number records the run; a second reservation for the same run, on any replica, is refused.
-- Without it a run dispatched again through the VIP -- its first build running on a replica that
-- had just died, not yet adopted -- was scheduled a second time on the new owner.
ALTER TABLE home_build_numbers ADD COLUMN run_id uuid;
CREATE UNIQUE INDEX home_build_numbers_run ON home_build_numbers (run_id) WHERE run_id IS NOT NULL;
