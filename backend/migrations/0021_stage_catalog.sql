-- 0021_stage_catalog.sql
-- The stage catalog as data. Built-in stages are the ones the shared pipeline implements;
-- custom stages are registered by a platform administrator and run a script that lives
-- in the application's repository (reviewed in git), anchored after a built-in stage.
-- Modules choose from this catalog through the portal; nobody edits a Jenkinsfile.

CREATE TABLE IF NOT EXISTS stage_catalog (
    id VARCHAR(64) PRIMARY KEY,
    name VARCHAR(120) NOT NULL,
    category VARCHAR(32) NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    kind VARCHAR(16) NOT NULL CHECK (kind IN ('builtin', 'custom')),
    -- Custom stages only: a repository-relative script path and the built-in stage
    -- after which it runs.
    script VARCHAR(255),
    after_stage VARCHAR(64) REFERENCES stage_catalog (id),
    -- A required stage cannot be removed from a module's pipeline: checkout, build,
    -- the supply-chain evidence stages and publish are what make an artifact deployable.
    required BOOLEAN NOT NULL DEFAULT FALSE,
    enabled_by_default BOOLEAN NOT NULL DEFAULT TRUE,
    position INTEGER NOT NULL,
    created_by VARCHAR(255) NOT NULL DEFAULT 'netci',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK ((kind = 'builtin' AND script IS NULL AND after_stage IS NULL)
        OR (kind = 'custom' AND script IS NOT NULL AND after_stage IS NOT NULL))
);

INSERT INTO stage_catalog (id, name, category, description, kind, required, enabled_by_default, position) VALUES
    ('checkout',           'Checkout source',      'source',   'Clone the commit netCI named; nothing else is built.',                 'builtin', TRUE,  TRUE, 10),
    ('unit-test',          'Unit tests',           'test',     'Run the template''s test script in the builder.',                      'builtin', FALSE, TRUE, 20),
    ('build',              'Build artifact/image', 'build',    'Build the image or binary from the checked-out source.',               'builtin', TRUE,  TRUE, 30),
    ('sbom',               'Generate SBOM',        'security', 'Syft SBOM of the artifact; netCI''s policy requires it.',              'builtin', TRUE,  TRUE, 40),
    ('vulnerability-scan', 'Vulnerability scan',   'security', 'Trivy scan; critical and high findings deny the artifact.',            'builtin', TRUE,  TRUE, 50),
    ('sign',               'Sign artifact',        'publish',  'Push to the registry and sign the digest with cosign.',                'builtin', TRUE,  TRUE, 60),
    ('publish',            'Publish artifact',     'publish',  'Check the evidence agrees with the artifact and submit it to netCI.',  'builtin', TRUE,  TRUE, 70),
    ('deploy',             'Deploy through netCI', 'deploy',   'netCI starts the deployment workflow once the artifact is admitted.',  'builtin', FALSE, TRUE, 80),
    ('health-check',       'Health check',         'verify',   'The runtime playbook''s health gate; a release that fails it is rolled back.', 'builtin', FALSE, TRUE, 90)
ON CONFLICT (id) DO NOTHING;
