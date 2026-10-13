-- ADR-076: what keeps the replicas' homes the same. One row per item (job, folder, view), versioned;
-- every change is also appended to home_changes, which every replica follows in order. The database
-- orders the writes -- copying files never did (ADR-074 E4: a save on one replica silently replaced
-- another's).
CREATE TABLE home_items (
    path       text        PRIMARY KEY,   -- the item's full name, e.g. "team/app"
    version    bigint      NOT NULL,      -- 1 when created, +1 per change; a delete is a version too
    deleted    boolean     NOT NULL DEFAULT false,
    content    bytea,                     -- the item's config.xml; NULL once deleted
    sha256     text,
    updated_by text        NOT NULL,      -- the replica, from its credential
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE home_changes (
    seq     bigserial   PRIMARY KEY,
    path    text        NOT NULL,
    version bigint      NOT NULL,
    deleted boolean     NOT NULL,
    origin  text        NOT NULL,
    at      timestamptz NOT NULL DEFAULT now()
);

-- A replica about to change an item holds its lock from catching up to publishing, so two
-- replicas' edits of one item are ordered as one Jenkins would order them. The token fences: a
-- holder whose lock expired and was taken cannot publish with it.
CREATE TABLE home_locks (
    path       text        PRIMARY KEY,
    holder     text        NOT NULL,
    token      bigint      NOT NULL,
    expires_at timestamptz NOT NULL
);
CREATE SEQUENCE home_lock_tokens;
