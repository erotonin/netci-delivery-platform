-- A newly registered system has no health evidence yet and must not start green.
ALTER TABLE systems DROP CONSTRAINT IF EXISTS systems_status_check;
ALTER TABLE systems ALTER COLUMN status SET DEFAULT 'unknown';
ALTER TABLE systems ADD CONSTRAINT systems_status_check
    CHECK (status IN ('unknown', 'healthy', 'degraded', 'critical'));
