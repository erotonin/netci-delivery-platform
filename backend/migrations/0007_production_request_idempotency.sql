-- Portal command idempotency must survive API restarts.
ALTER TABLE production_requests ADD COLUMN IF NOT EXISTS idempotency_key VARCHAR(128);
ALTER TABLE production_requests ADD COLUMN IF NOT EXISTS request_hash CHAR(64);
CREATE UNIQUE INDEX IF NOT EXISTS production_requests_idempotency_idx
    ON production_requests (idempotency_key) WHERE idempotency_key IS NOT NULL;
