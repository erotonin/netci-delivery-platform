-- Production requests track the terminal result of the deployment they created.
ALTER TABLE production_requests DROP CONSTRAINT IF EXISTS production_requests_status_check;
ALTER TABLE production_requests ADD CONSTRAINT production_requests_status_check
    CHECK (status IN ('waiting_approval', 'approved', 'rejected', 'blocked', 'succeeded'));
