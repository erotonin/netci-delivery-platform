-- Shared pipelines (ADR-058): CI definitions the platform owns, picked by modules by name.
-- A pipeline is one script; each saved script is a new version that a second platform
-- administrator approves before any module runs it. At most one version is active.
CREATE TABLE IF NOT EXISTS shared_pipelines (
    name VARCHAR(63) PRIMARY KEY CHECK (name ~ '^[a-z][a-z0-9-]{1,62}$'),
    description TEXT NOT NULL DEFAULT '',
    created_by VARCHAR(255) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS shared_pipeline_versions (
    pipeline_name VARCHAR(63) NOT NULL REFERENCES shared_pipelines (name),
    version INTEGER NOT NULL CHECK (version >= 1),
    script TEXT NOT NULL,
    script_sha256 CHAR(64) NOT NULL,
    -- [{"id", "name", "builtin"}], derived from the script when it was validated.
    stages JSONB NOT NULL,
    status VARCHAR(16) NOT NULL CHECK (status IN ('proposed', 'active', 'rejected', 'superseded')),
    created_by VARCHAR(255) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    decided_by VARCHAR(255),
    decided_at TIMESTAMPTZ,
    rejection_reason TEXT,
    -- The approver is a different person: enforced by the service, which is also what
    -- relaxes it when authentication is off (every caller the same subject), as for
    -- custom stages and production approvals.
    PRIMARY KEY (pipeline_name, version)
);

-- Two approvals racing must not leave two active versions.
CREATE UNIQUE INDEX IF NOT EXISTS shared_pipeline_versions_one_active
    ON shared_pipeline_versions (pipeline_name) WHERE status = 'active';

-- The pipeline a module's runs use, by name; resolved to its active version per run.
ALTER TABLE applications
    ADD COLUMN IF NOT EXISTS shared_pipeline VARCHAR(63) REFERENCES shared_pipelines (name);
