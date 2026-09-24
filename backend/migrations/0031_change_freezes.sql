-- Change freezes as records with a start and an end (ADR-047). Until now the calendar had
-- to say "netCI records no maintenance windows": a freeze lived in a chat message, and a
-- deployment during one was stopped only if someone remembered. A freeze here refuses
-- any deployment that would start inside it in its environments and scope; rollbacks are
-- never refused, because a freeze exists to protect service, not to prevent restoring it.
CREATE TABLE IF NOT EXISTS change_freezes (
    id UUID PRIMARY KEY,
    name TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 120),
    starts_at TIMESTAMPTZ NOT NULL,
    ends_at TIMESTAMPTZ NOT NULL,
    environments TEXT[] NOT NULL CHECK (cardinality(environments) > 0
        AND environments <@ ARRAY['dev', 'staging', 'prod']::TEXT[]),
    system_id TEXT,
    module_id TEXT,
    reason TEXT NOT NULL CHECK (length(reason) BETWEEN 1 AND 1000),
    created_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    cancelled_at TIMESTAMPTZ,
    cancelled_by TEXT,
    CHECK (ends_at > starts_at),
    CHECK ((cancelled_at IS NULL) = (cancelled_by IS NULL))
);
CREATE INDEX IF NOT EXISTS idx_change_freezes_window ON change_freezes (ends_at, starts_at) WHERE cancelled_at IS NULL;
