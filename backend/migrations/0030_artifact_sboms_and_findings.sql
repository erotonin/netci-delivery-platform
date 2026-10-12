-- What is inside each artifact, and which known vulnerabilities it carries (ADR-045).
-- netCI used to keep an SBOM only as a *path on the build agent*, which the agent took
-- with it when it was destroyed, so "where does CVE-X run?" had no answer once a CVE was
-- published after the build. The SBOM is now kept, keyed by the immutable digest, and
-- netCI re-scans it itself.
CREATE TABLE IF NOT EXISTS artifact_sboms (
    artifact_digest TEXT PRIMARY KEY CHECK (artifact_digest ~ '^sha256:[0-9a-f]{64}$'),
    application_id UUID NOT NULL REFERENCES applications(id),
    pipeline_run_id UUID NOT NULL REFERENCES pipeline_runs(id),
    format TEXT NOT NULL,
    document JSONB NOT NULL,
    component_count INTEGER NOT NULL CHECK (component_count >= 0),
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- One row per (digest, where it was found, vulnerability, package, version). `ci` rows
-- are the build's own scan; `rescan` rows are netCI's later scans of the stored SBOM,
-- replaced as a set each time so a fixed database entry disappears.
CREATE TABLE IF NOT EXISTS artifact_findings (
    artifact_digest TEXT NOT NULL,
    source TEXT NOT NULL CHECK (source IN ('ci', 'rescan')),
    vulnerability_id TEXT NOT NULL,
    package TEXT NOT NULL DEFAULT '',
    installed_version TEXT NOT NULL DEFAULT '',
    severity TEXT NOT NULL,
    fixed_version TEXT NOT NULL DEFAULT '',
    first_seen_at TIMESTAMPTZ NOT NULL,
    last_seen_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (artifact_digest, source, vulnerability_id, package, installed_version)
);
CREATE INDEX IF NOT EXISTS idx_artifact_findings_vulnerability
    ON artifact_findings (vulnerability_id);

-- When netCI last scanned a digest's SBOM, and whether that scan ran at all. A failed
-- scan is recorded as failed: an exposure answer must be able to say "not covered"
-- rather than read an absent scan as a clean one.
CREATE TABLE IF NOT EXISTS artifact_rescans (
    artifact_digest TEXT PRIMARY KEY,
    scanned_at TIMESTAMPTZ NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('scanned', 'failed')),
    scanner TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT ''
);
