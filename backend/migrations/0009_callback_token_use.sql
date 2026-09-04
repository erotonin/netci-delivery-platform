-- Record every callback token that is actually used.
--
-- Two things need this. A token whose scope is terminal -- reporting a deployment
-- result -- must be usable once: a token scraped from a worker's environment could
-- otherwise be replayed later to overwrite a newer result with an older one. And a
-- security review needs to be able to answer "which workload wrote this?" from the
-- database rather than from a log that may have rotated away.
--
-- Only the `jti` is stored. The token, its signature and the signing key never reach
-- this table, so a database dump cannot be turned back into a working credential.
CREATE TABLE IF NOT EXISTS callback_token_uses (
    jti CHAR(32) PRIMARY KEY,
    workload VARCHAR(32) NOT NULL,
    application_id UUID NOT NULL REFERENCES applications(id),
    pipeline_run_id UUID REFERENCES pipeline_runs(id),
    deployment_id UUID REFERENCES deployments(id),
    operation VARCHAR(64) NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    used_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT callback_token_uses_names_one_resource
        CHECK ((pipeline_run_id IS NULL) <> (deployment_id IS NULL))
);

-- Expired rows are worthless once the token they describe can no longer be presented;
-- this index is what makes the periodic delete cheap.
CREATE INDEX IF NOT EXISTS callback_token_uses_expires_at_idx
    ON callback_token_uses (expires_at);
