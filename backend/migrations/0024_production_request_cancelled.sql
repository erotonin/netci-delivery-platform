-- 0024_production_request_cancelled.sql
-- A production request whose deployment was cancelled (by the requester or an
-- operator, before the release ran) is neither rejected nor blocked: nobody refused it
-- and nothing failed. It is `cancelled`, and says so.
ALTER TABLE production_requests DROP CONSTRAINT IF EXISTS production_requests_status_check;
ALTER TABLE production_requests ADD CONSTRAINT production_requests_status_check
    CHECK (status IN ('waiting_approval', 'approved', 'rejected', 'blocked', 'succeeded', 'cancelled'));
