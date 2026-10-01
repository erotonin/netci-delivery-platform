-- ADR-065: blocks that must not run twice. A row is written before the block's body runs.
CREATE TABLE once_markers (
    client     text        NOT NULL,   -- the cell that asked, from its credential
    scope      text        NOT NULL,   -- the build: controller instance, job, number
    key        text        NOT NULL,   -- the block's name within the build
    nonce      text        NOT NULL,   -- the attempt that recorded it
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (client, scope, key)
);
