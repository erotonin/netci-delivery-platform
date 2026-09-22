-- "What is this environment running right now?" is asked on every deployment netCI
-- creates -- a CI result, a redeploy, a production promotion, a rollback -- and it was
-- answered by loading every deployment the application has ever had and picking the
-- newest settled one in Python. Nothing thins `deployments`: retention covers console
-- lines, delivery events, notifications and spent callback tokens, not the durable
-- record. So each release of a long-lived service read every release that came before it.
CREATE INDEX IF NOT EXISTS idx_deployments_settled_per_environment
    ON deployments (application_id, environment, updated_at DESC)
    WHERE status IN ('healthy', 'rolled_back');
