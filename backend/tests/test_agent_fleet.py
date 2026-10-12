"""Edge agents across replicas (ADR-032): connections and commands are rows, not dicts.

Two `AgentFleet` instances over one database stand for two API replicas. What has to
hold: a replica sees agents it does not hold; a command for an agent held elsewhere is
claimed exactly once, by the holder; a replica's late goodbye cannot erase a fresh
connection to another replica; the singleton loops take turns through the advisory lock.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
from datetime import timedelta
from uuid import uuid4

import pytest

from app import agent_fleet
from app.agent_fleet import LOCK_RECONCILE, AgentFleet, run_exclusively
from app.store import PostgresDatabase
from app.store.memory import InMemoryDatabase


@pytest.fixture
def db():
    return InMemoryDatabase()


def test_a_replica_sees_agents_held_by_another(db):
    a, b = AgentFleet(db, "api-a"), AgentFleet(db, "api-b")
    a.register("edge-01", "runner", "jti-1")
    seen = {c["hostname"]: c for c in b.connections()}
    assert seen["edge-01"]["replicaId"] == "api-a"
    assert seen["edge-01"]["local"] is False and seen["edge-01"]["stale"] is False


def test_a_command_for_an_agent_held_elsewhere_is_claimed_by_the_holder_exactly_once(db):
    a, b = AgentFleet(db, "api-a"), AgentFleet(db, "api-b")
    a.register("edge-01", "runner", "jti-1")
    command = b.submit("edge-01", "uptime", "pat", timeout_seconds=30)
    # The holder claims; a second claim finds nothing left.
    first = a.claim(["edge-01"])
    assert [c.id for c in first] == [command.id] and first[0].claimed_by == "api-a"
    assert a.claim(["edge-01"]) == ()
    assert a.complete(command.id, {"output": "up 3 days", "exitCode": 0})
    answered = b.get(command.id)
    assert answered.status == "completed" and answered.result["output"] == "up 3 days"


def test_the_requesting_replica_waits_for_the_answer(db):
    a, b = AgentFleet(db, "api-a"), AgentFleet(db, "api-b")
    a.register("edge-01", "runner", "jti-1")
    command = b.submit("edge-01", "uptime", "pat", timeout_seconds=5)

    def holder_answers():
        claimed = a.claim(["edge-01"])
        a.complete(claimed[0].id, {"output": "ok", "exitCode": 0})

    timer = threading.Timer(0.3, holder_answers)
    timer.start()
    answered = asyncio.run(b.wait(command.id, timeout_seconds=5, poll_seconds=0.05))
    assert answered.status == "completed" and answered.claimed_by == "api-a"


def test_an_unanswered_command_expires_instead_of_hanging(db):
    b = AgentFleet(db, "api-b")
    AgentFleet(db, "api-a").register("edge-01", "runner", "jti-1")
    command = b.submit("edge-01", "uptime", "pat", timeout_seconds=0.2)
    answered = asyncio.run(b.wait(command.id, timeout_seconds=0.3, poll_seconds=0.05))
    assert answered.status == "expired"
    # Nobody can claim an expired command afterwards.
    assert AgentFleet(db, "api-a").claim(["edge-01"]) == ()


def test_a_late_goodbye_from_the_old_replica_does_not_erase_a_fresh_connection(db):
    a, b = AgentFleet(db, "api-a"), AgentFleet(db, "api-b")
    a.register("edge-01", "runner", "jti-1")
    b.register("edge-01", "runner", "jti-2")  # the agent reconnected to b
    assert a.unregister("edge-01") is False  # a's socket closed late; the row is b's now
    assert a.touch("edge-01") is False
    assert {c["hostname"]: c["replicaId"] for c in a.connections()} == {"edge-01": "api-b"}


def test_a_replica_that_stopped_refreshing_shows_as_stale(db, monkeypatch):
    a = AgentFleet(db, "api-a", stale_after_seconds=1)
    a.register("edge-01", "runner", "jti-1")
    later = agent_fleet._now() + timedelta(seconds=5)
    monkeypatch.setattr(agent_fleet, "_now", lambda: later)
    assert a.connections()[0]["stale"] is True


def test_run_exclusively_runs_the_work_when_the_lock_is_free(db):
    calls = []
    assert run_exclusively(db, LOCK_RECONCILE, lambda: calls.append(1) or "done", describe="test") == "done"
    assert calls == [1]


def test_replica_identity_is_configurable(monkeypatch):
    monkeypatch.setenv("NETCI_REPLICA_ID", "api-blue")
    assert agent_fleet.replica_identity() == "api-blue"
    monkeypatch.delenv("NETCI_REPLICA_ID")
    assert ":" in agent_fleet.replica_identity()


# ------------------------------------------------------------ PostgreSQL semantics


DATABASE_URL = os.getenv("NETCI_TEST_DATABASE_URL", "").strip()
needs_postgres = pytest.mark.skipif(not DATABASE_URL, reason="set NETCI_TEST_DATABASE_URL to a migrated PostgreSQL")


@pytest.fixture
def pg():
    import psycopg

    def wipe():
        with psycopg.connect(DATABASE_URL) as connection, connection.cursor() as cursor:
            cursor.execute("TRUNCATE agent_connections, agent_commands")

    wipe()
    db = PostgresDatabase(DATABASE_URL)
    yield db
    db.close()
    wipe()


@needs_postgres
def test_postgres_claims_are_exclusive_across_connections(pg):
    """`FOR UPDATE SKIP LOCKED`: two replicas claiming at once split the pending rows."""

    a, b = AgentFleet(pg, "api-a"), AgentFleet(pg, "api-b")
    a.register("edge-01", "runner", "jti-1")
    ids = {b.submit("edge-01", f"uptime #{i}", "pat", timeout_seconds=30).id for i in range(20)}
    results: dict[str, list] = {"a": [], "b": []}

    def claim_all(name, fleet):
        while True:
            claimed = fleet.claim(["edge-01"])
            if not claimed:
                break
            results[name].extend(c.id for c in claimed)

    threads = [threading.Thread(target=claim_all, args=("a", a)), threading.Thread(target=claim_all, args=("b", b))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results["a"] + results["b"]) == sorted(ids)
    assert not set(results["a"]) & set(results["b"])


@needs_postgres
def test_postgres_advisory_lock_lets_exactly_one_replica_run_the_pass(pg):
    """The second replica finds the lock taken while the first is still inside its pass."""

    entered = threading.Event()
    release = threading.Event()
    outcomes = {}

    def slow_pass():
        entered.set()
        release.wait(5)
        return "ran"

    def first():
        outcomes["first"] = run_exclusively(pg, LOCK_RECONCILE, slow_pass, describe="first")

    t = threading.Thread(target=first)
    t.start()
    assert entered.wait(5)
    outcomes["second"] = run_exclusively(pg, LOCK_RECONCILE, lambda: "ran", describe="second")
    release.set()
    t.join()
    assert outcomes == {"first": "ran", "second": None}
    # Released with the transaction: the next pass runs.
    assert run_exclusively(pg, LOCK_RECONCILE, lambda: "ran", describe="third") == "ran"


@needs_postgres
def test_postgres_result_round_trips_as_json(pg):
    a = AgentFleet(pg, "api-a")
    command = a.submit("edge-02", "df -h", "pat", timeout_seconds=30)
    a.claim(["edge-02"])
    a.complete(command.id, {"output": "Filesystem\n/dev/sda1", "exitCode": 0, "answeredBy": "api-a"})
    stored = a.get(command.id)
    assert stored.result == {"output": "Filesystem\n/dev/sda1", "exitCode": 0, "answeredBy": "api-a"}
    assert json.dumps(stored.result)
    assert stored.id == command.id and isinstance(stored.id, type(uuid4()))
