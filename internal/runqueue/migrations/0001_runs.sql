-- ADR-063: the durable run queue. A row exists before any trigger is acknowledged.
CREATE TABLE runs (
    id              uuid PRIMARY KEY,
    client          text        NOT NULL,           -- decided by the server from the credential
    idempotency_key text,                            -- e.g. a webhook delivery id, per client
    cell            text        NOT NULL,           -- decided by the server from the job
    job             text        NOT NULL,
    parameters      jsonb       NOT NULL DEFAULT '{}'::jsonb,
    state           text        NOT NULL DEFAULT 'accepted'
                    CHECK (state IN ('accepted', 'dispatched', 'started', 'finished', 'cancelled', 'refused')),
    session         text,                            -- controller start that last acknowledged it
    queue_id        bigint,
    build_number    integer,
    build_url       text,
    result          text,
    attempts        integer     NOT NULL DEFAULT 0,
    last_error      text,
    accepted_at     timestamptz NOT NULL DEFAULT now(),
    dispatched_at   timestamptz,
    started_at      timestamptz,
    finished_at     timestamptz,
    next_attempt_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT runs_idempotency UNIQUE (client, idempotency_key),
    CONSTRAINT runs_build CHECK (state NOT IN ('started', 'finished') OR build_number IS NOT NULL)
);

-- What the dispatcher scans: the open runs of a cell, oldest first.
CREATE INDEX runs_open ON runs (cell, state, next_attempt_at) WHERE state IN ('accepted', 'dispatched', 'started');

-- Every transition, in the transaction that made it.
CREATE TABLE run_events (
    id      bigserial   PRIMARY KEY,
    run_id  uuid        NOT NULL REFERENCES runs (id),
    at      timestamptz NOT NULL DEFAULT now(),
    from_state text,
    to_state   text     NOT NULL,
    detail  jsonb       NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX run_events_run ON run_events (run_id, id);
