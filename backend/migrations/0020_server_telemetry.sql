-- 0020_server_telemetry.sql
-- Telemetry reported by edge agents decides the pre-flight deployment gate, so it is
-- durable state and lives here, not in a process-local dict that a second API replica
-- cannot see and a restart forgets. One row per server: the latest observation.

CREATE TABLE IF NOT EXISTS server_telemetry (
    server_name VARCHAR(255) PRIMARY KEY,
    cpu_percent DOUBLE PRECISION NOT NULL,
    mem_percent DOUBLE PRECISION NOT NULL,
    disk_percent DOUBLE PRECISION NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL
);
