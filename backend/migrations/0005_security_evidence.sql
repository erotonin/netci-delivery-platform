-- Durable supply-chain evidence.
--
-- Deployment policy is evaluated again after process restarts, so retaining only the
-- allow/deny audit decision is insufficient: the signed digest, SBOM and vulnerability
-- report that produced the decision must remain available as one authoritative record.
CREATE TABLE IF NOT EXISTS security_evidence (
    pipeline_run_id UUID PRIMARY KEY REFERENCES pipeline_runs(id) ON DELETE CASCADE,
    application_id UUID NOT NULL REFERENCES applications(id),
    artifact_digest TEXT NOT NULL CHECK (artifact_digest ~ '^sha256:[0-9a-f]{64}$'),
    evidence JSONB NOT NULL,
    decision VARCHAR(16) NOT NULL CHECK (decision IN ('allow', 'deny')),
    reason TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS security_evidence_application_idx
    ON security_evidence (application_id, updated_at DESC);
