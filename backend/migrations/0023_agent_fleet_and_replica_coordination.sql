-- 0023_agent_fleet_and_replica_coordination.sql
-- Edge agents connect to *one* API replica over a websocket. Which replica holds which
-- agent, and which diagnostic command is waiting for which agent, used to live in that
-- replica's memory: a second replica saw no agents and could dispatch nothing, and a
-- restart forgot every connection. Both facts are durable state now, so any replica can
-- answer "who is connected" and accept a command for an agent held elsewhere (ADR-032).

CREATE TABLE IF NOT EXISTS agent_connections (
    hostname      VARCHAR(253) PRIMARY KEY,
    agent_id      VARCHAR(128) NOT NULL,
    replica_id    VARCHAR(128) NOT NULL,
    token_jti     VARCHAR(64)  NOT NULL,
    connected_at  TIMESTAMPTZ  NOT NULL,
    last_seen_at  TIMESTAMPTZ  NOT NULL
);

CREATE INDEX IF NOT EXISTS agent_connections_replica_idx ON agent_connections (replica_id);

-- One row per requested command. `pending` until the replica holding the agent claims it
-- (`sent`), then `completed`/`failed` with the agent's answer, or `expired` when nothing
-- claimed or answered it before `expires_at`. The requesting replica polls the row.
CREATE TABLE IF NOT EXISTS agent_commands (
    id            UUID PRIMARY KEY,
    hostname      VARCHAR(253) NOT NULL,
    command       TEXT         NOT NULL,
    requested_by  VARCHAR(255) NOT NULL,
    status        VARCHAR(16)  NOT NULL CHECK (status IN ('pending', 'sent', 'completed', 'failed', 'expired')),
    result        JSONB,
    created_at    TIMESTAMPTZ  NOT NULL,
    expires_at    TIMESTAMPTZ  NOT NULL,
    claimed_by    VARCHAR(128),
    claimed_at    TIMESTAMPTZ,
    completed_at  TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS agent_commands_pending_idx ON agent_commands (hostname, status) WHERE status = 'pending';
