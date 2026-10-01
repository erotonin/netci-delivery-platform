-- ADR-064: build sandboxes. A row exists before its pod; the agent's secret is never stored.
CREATE TABLE sandboxes (
    id          uuid PRIMARY KEY,
    pool        text        NOT NULL,
    pod         text        NOT NULL UNIQUE,
    pod_uid     text,
    state       text        NOT NULL
                CHECK (state IN ('creating', 'warm', 'claimed', 'bound', 'released', 'deleted', 'failed')),
    cold        boolean     NOT NULL DEFAULT false,   -- claimed before it was warm
    cell        text,
    agent       text,
    controller  text,
    reason      text,
    created_at  timestamptz NOT NULL DEFAULT now(),
    warm_at     timestamptz,
    claimed_at  timestamptz,
    bound_at    timestamptz,
    released_at timestamptz,
    ended_at    timestamptz,
    CONSTRAINT sandboxes_claim CHECK (state NOT IN ('claimed', 'bound') OR (cell IS NOT NULL AND agent IS NOT NULL))
);
CREATE INDEX sandboxes_live ON sandboxes (pool, state) WHERE state NOT IN ('deleted');

CREATE TABLE sandbox_events (
    id         bigserial   PRIMARY KEY,
    sandbox_id uuid        NOT NULL REFERENCES sandboxes (id),
    at         timestamptz NOT NULL DEFAULT now(),
    from_state text,
    to_state   text        NOT NULL,
    detail     jsonb       NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX sandbox_events_sandbox ON sandbox_events (sandbox_id, id);
