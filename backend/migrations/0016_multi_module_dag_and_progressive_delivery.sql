-- 0016_multi_module_dag_and_progressive_delivery.sql
-- Phase 10 (P2.1): Multi-Module DAG Release Plan, SAGA Orchestration, and Progressive Delivery

-- Enhance production_requests with release plan DAG and deployment strategy
ALTER TABLE production_requests
    ADD COLUMN IF NOT EXISTS release_plan JSONB,
    ADD COLUMN IF NOT EXISTS strategy VARCHAR(32) NOT NULL DEFAULT 'rolling',
    ADD COLUMN IF NOT EXISTS strategy_config JSONB NOT NULL DEFAULT '{}'::jsonb;

-- Enhance production_request_modules with dependencies, execution state, and deployment reference
ALTER TABLE production_request_modules
    ADD COLUMN IF NOT EXISTS dependencies TEXT[] NOT NULL DEFAULT '{}',
    ADD COLUMN IF NOT EXISTS status VARCHAR(32) NOT NULL DEFAULT 'pending',
    ADD COLUMN IF NOT EXISTS deployment_id UUID REFERENCES deployments(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS started_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS completed_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS error_message TEXT;

-- Enhance deployments with progressive delivery attributes (traffic weighting and blue-green color)
ALTER TABLE deployments
    ADD COLUMN IF NOT EXISTS strategy VARCHAR(32) NOT NULL DEFAULT 'rolling',
    ADD COLUMN IF NOT EXISTS traffic_weight INTEGER NOT NULL DEFAULT 100,
    ADD COLUMN IF NOT EXISTS active_color VARCHAR(16),
    ADD COLUMN IF NOT EXISTS canary_step INTEGER NOT NULL DEFAULT 0;

-- Indexes for efficient lookups by coordinator and status
CREATE INDEX IF NOT EXISTS idx_prm_request_status ON production_request_modules (request_id, status);
CREATE INDEX IF NOT EXISTS idx_prm_deployment_id ON production_request_modules (deployment_id) WHERE deployment_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_deployments_strategy ON deployments (strategy);
