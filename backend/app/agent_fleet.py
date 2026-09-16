"""Edge agents across API replicas (ADR-032).

An agent keeps one outbound websocket to one API replica. Two facts about it must be
visible to every replica: that it is connected (and where), and that an operator asked
it to run a command. Both live in the platform database:

  agent_connections   one row per hostname, owned by the replica holding the socket,
                      refreshed on every heartbeat; stale rows are reported as such, not
                      hidden, because a replica that died without a goodbye leaves one.
  agent_commands      one row per request; the holding replica claims it (exactly once,
                      `FOR UPDATE SKIP LOCKED`), sends it down the socket, writes the
                      answer; the requesting replica polls the row until it is answered
                      or expires.

The websocket itself stays process-local -- a socket cannot be shared -- but nothing
else about the agent does.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

from .domain.models import AgentCommand, AgentConnection

logger = logging.getLogger(__name__)

# Keys for pg_try_advisory_xact_lock: arbitrary but fixed, one per singleton loop.
LOCK_RECONCILE = 0x6E65_7463_0001
LOCK_OUTBOX = 0x6E65_7463_0002


def replica_identity() -> str:
    """This process's name among the replicas, from the platform's own configuration."""

    configured = os.getenv("NETCI_REPLICA_ID", "").strip()
    return configured or f"{socket.gethostname()}:{os.getpid()}"


def _now() -> datetime:
    return datetime.now(timezone.utc)


class AgentFleet:
    def __init__(self, database: Any, replica_id: str | None = None, *, stale_after_seconds: float = 90.0) -> None:
        self.database = database
        self.replica_id = replica_id or replica_identity()
        self.stale_after = timedelta(seconds=stale_after_seconds)

    # -------------------------------------------------------------- connections

    def register(self, hostname: str, agent_id: str, token_jti: str) -> AgentConnection:
        now = _now()
        connection = AgentConnection(
            hostname=hostname, agent_id=agent_id, replica_id=self.replica_id, token_jti=token_jti,
            connected_at=now, last_seen_at=now,
        )
        with self.database.transaction() as session:
            session.upsert_agent_connection(connection)
        return connection

    def touch(self, hostname: str) -> bool:
        with self.database.transaction() as session:
            return session.touch_agent_connection(hostname, self.replica_id, _now())

    def unregister(self, hostname: str) -> bool:
        with self.database.transaction() as session:
            return session.delete_agent_connection(hostname, self.replica_id)

    def connections(self) -> list[dict[str, Any]]:
        """Every agent any replica holds, with `stale` when its replica stopped refreshing it."""

        now = _now()
        with self.database.transaction() as session:
            rows = session.list_agent_connections()
        return [
            {
                "hostname": row.hostname,
                "agentId": row.agent_id,
                "replicaId": row.replica_id,
                "connectedAt": row.connected_at.isoformat(),
                "lastSeenAt": row.last_seen_at.isoformat(),
                "stale": (now - row.last_seen_at) > self.stale_after,
                "local": row.replica_id == self.replica_id,
            }
            for row in rows
        ]

    # ----------------------------------------------------------------- commands

    def submit(self, hostname: str, command: str, requested_by: str, timeout_seconds: float) -> AgentCommand:
        now = _now()
        record = AgentCommand(
            id=uuid4(), hostname=hostname, command=command, requested_by=requested_by,
            status="pending", created_at=now, expires_at=now + timedelta(seconds=timeout_seconds),
        )
        with self.database.transaction() as session:
            session.insert_agent_command(record)
        return record

    def claim(self, hostnames: list[str]) -> tuple[AgentCommand, ...]:
        with self.database.transaction() as session:
            return session.claim_agent_commands(self.replica_id, hostnames, _now())

    def complete(self, command_id: UUID, result: dict[str, Any], *, failed: bool = False) -> bool:
        with self.database.transaction() as session:
            return session.complete_agent_command(command_id, "failed" if failed else "completed", result, _now())

    def get(self, command_id: UUID) -> AgentCommand | None:
        with self.database.transaction() as session:
            return session.agent_command(command_id)

    def expire(self) -> int:
        with self.database.transaction() as session:
            return session.expire_agent_commands(_now())

    async def wait(self, command_id: UUID, timeout_seconds: float, poll_seconds: float = 0.25) -> AgentCommand | None:
        """Poll until the command is answered or its deadline passes. None means timed out."""

        deadline = asyncio.get_running_loop().time() + timeout_seconds
        while True:
            record = await asyncio.to_thread(self.get, command_id)
            if record is not None and record.status in ("completed", "failed", "expired"):
                return record
            if asyncio.get_running_loop().time() >= deadline:
                await asyncio.to_thread(self.expire)
                return await asyncio.to_thread(self.get, command_id)
            await asyncio.sleep(poll_seconds)


def run_exclusively(database: Any, key: int, work, *, describe: str) -> Any | None:
    """Run `work()` while holding the advisory lock `key`; skip (None) if another replica has it.

    The lock is transaction-scoped on a connection held open around the work. The work
    itself opens its own transactions from the pool -- the guard connection carries only
    the lock.
    """

    with database.transaction() as guard:
        if not guard.try_advisory_lock(key):
            logger.debug("%s: another replica holds the lock; skipping this pass", describe)
            return None
        return work()
