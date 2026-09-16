-- 0022_stage_catalog_governance_and_parameters.sql
-- A custom stage is code that runs on every build agent of every module that selects
-- it. Registering one therefore takes two platform administrators: the one who proposes
-- it and a different one who approves it (the same separation of duties as production
-- approvals). Custom stages may also declare parameters a module sets per pipeline.

ALTER TABLE stage_catalog
    ADD COLUMN IF NOT EXISTS status VARCHAR(16) NOT NULL DEFAULT 'active'
        CHECK (status IN ('proposed', 'active', 'rejected')),
    ADD COLUMN IF NOT EXISTS approved_by VARCHAR(255),
    ADD COLUMN IF NOT EXISTS parameters JSONB NOT NULL DEFAULT '[]'::jsonb;

-- Values a module set for its custom stages: {"<stage id>": {"<NAME>": "<value>"}}.
ALTER TABLE applications
    ADD COLUMN IF NOT EXISTS stage_parameters JSONB NOT NULL DEFAULT '{}'::jsonb;
