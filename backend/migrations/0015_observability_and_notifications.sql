-- Migration 0015: Observability, Transactional Notifications Outbox, and Pagination Indexes

CREATE TABLE IF NOT EXISTS notifications (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    event_type VARCHAR(64) NOT NULL,
    aggregate_type VARCHAR(32) NOT NULL,
    aggregate_id VARCHAR(64) NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    recipient VARCHAR(255) NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'delivered', 'failed', 'dead_letter')),
    attempt INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 5,
    last_attempt_at TIMESTAMPTZ,
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    delivered_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_notifications_outbox
    ON notifications (status, next_attempt_at)
    WHERE status IN ('pending', 'failed');

CREATE INDEX IF NOT EXISTS idx_notifications_aggregate
    ON notifications (aggregate_type, aggregate_id);

CREATE INDEX IF NOT EXISTS idx_notifications_cursor
    ON notifications (created_at DESC, id DESC);

CREATE INDEX IF NOT EXISTS idx_pipeline_runs_cursor
    ON pipeline_runs (created_at DESC, id DESC);

CREATE INDEX IF NOT EXISTS idx_deployments_cursor
    ON deployments (created_at DESC, id DESC);

CREATE INDEX IF NOT EXISTS idx_audit_events_cursor
    ON audit_events (occurred_at DESC, id DESC);

CREATE INDEX IF NOT EXISTS idx_callback_token_uses_expires
    ON callback_token_uses (expires_at);
