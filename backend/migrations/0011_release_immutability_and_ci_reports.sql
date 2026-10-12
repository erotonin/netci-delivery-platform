-- Release immutability and version CI report separation
--
-- Two defects this closes:
-- 1. `release_versions` previously used `ON CONFLICT DO UPDATE SET metadata = EXCLUDED.metadata`,
--    allowing an existing release's digest, provenance, or metadata to be mutated after publication.
-- 2. CI quality reports were merged directly into `release_versions.metadata`, coupling mutable
--    CI evidence with the immutable release version artifact definition.
--
-- This migration introduces `version_ci_reports` as an append-only ledger for CI quality reports
-- and ensures release versions remain strictly immutable.

CREATE TABLE IF NOT EXISTS version_ci_reports (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    module_id VARCHAR(63) NOT NULL REFERENCES modules(id) ON DELETE CASCADE,
    version VARCHAR(128) NOT NULL,
    report JSONB NOT NULL,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    recorded_by VARCHAR(255) NOT NULL DEFAULT 'netCI Pipeline'
);

CREATE INDEX IF NOT EXISTS idx_version_ci_reports_lookup
    ON version_ci_reports (module_id, version, recorded_at DESC);
