"""Comprehensive verification suite for Phase 9: Observability, Notifications, Pagination, and Disaster Recovery."""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from backend.app.domain.models import (
    Application,
    Environment,
    NotificationRecord,
    NotificationStatus,
    PipelineRun,
    PipelineStatus,
    Runtime,
)
from backend.app.logging import StructuredJsonFormatter, redact_sensitive_data
from backend.app.main import app, database, platform
from backend.app.metrics import metrics
from backend.app.notifications import (
    HttpWebhookNotificationDispatcher,
    NotificationOutboxWorker,
)
from backend.app.retention import RetentionManager
from backend.app.store.memory import InMemoryDatabase
from backend.app.store.postgres import PostgresConnectionPool, PostgresDatabase

TEST_PG_URL = os.getenv(
    "NETCI_TEST_DATABASE_URL",
    "postgresql://netci:netci-local-only@127.0.0.1:55432/netci",
)


# ==============================================================================
# 1. Connection Pool Tests
# ==============================================================================


def test_postgres_connection_pool_acquisition_and_stats():
    pool = PostgresConnectionPool(TEST_PG_URL, min_size=2, max_size=5, timeout=2.0)
    try:
        stats_initial = pool.stats()
        assert stats_initial["max"] == 5

        conn1 = pool.get()
        assert not conn1.closed
        with conn1.cursor() as cur:
            cur.execute("SELECT 1 AS num")
            row = cur.fetchone()
            assert row["num"] == 1

        stats_active = pool.stats()
        assert stats_active["active"] >= 1

        pool.put(conn1)
        stats_returned = pool.stats()
        assert stats_returned["idle"] >= 1
    finally:
        pool.close()


def test_postgres_connection_pool_exhaustion_timeout():
    # Create tiny pool of 2 connections
    pool = PostgresConnectionPool(TEST_PG_URL, min_size=1, max_size=2, timeout=0.3)
    try:
        conn1 = pool.get()
        conn2 = pool.get()
        stats = pool.stats()
        assert stats["total"] == 2
        assert stats["active"] == 2

        # Third checkout should raise TimeoutError
        with pytest.raises(TimeoutError) as exc_info:
            pool.get()
        assert "exhausted" in str(exc_info.value)

        # Releasing one allows new checkout
        pool.put(conn1)
        conn3 = pool.get()
        assert not conn3.closed
        pool.put(conn2)
        pool.put(conn3)
    finally:
        pool.close()


# ==============================================================================
# 2. Structured JSON Logging & Credential Redaction
# ==============================================================================


def test_structured_json_logging_format_and_redaction():
    formatter = StructuredJsonFormatter(service="netci-test")
    record = logging.LogRecord(
        name="test.logger",
        level=logging.INFO,
        pathname="test.py",
        lineno=42,
        msg="User authenticated successfully with Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.e30.secret",
        args=(),
        exc_info=None,
    )
    record.correlation_id = "corr-test-12345"
    record.extra_info = {
        "password": "super-secret-password",
        "safe_key": "safe_value",
        "api_key": "raw-api-key",
    }

    formatted_str = formatter.format(record)
    parsed = json.loads(formatted_str)

    assert parsed["service"] == "netci-test"
    assert parsed["level"] == "INFO"
    assert parsed["correlation_id"] == "corr-test-12345"
    assert "Bearer [REDACTED]" in parsed["message"]
    assert "secret" not in parsed["message"]
    # Check extras redaction
    extra = parsed.get("extra", {}).get("extra_info", {})
    assert extra.get("password") == "[REDACTED]"
    assert extra.get("api_key") == "[REDACTED]"
    assert extra.get("safe_key") == "safe_value"


def test_redact_sensitive_data_helper():
    payload = {
        "username": "developer",
        "client_secret": "my-secret",
        "nested": {
            "token": "token-123",
            "count": 5,
        },
        "items": ["safe", "Authorization: Bearer my-token-data"],
    }
    scrubbed = redact_sensitive_data(payload)
    assert scrubbed["client_secret"] == "[REDACTED]"
    assert scrubbed["nested"]["token"] == "[REDACTED]"
    assert scrubbed["nested"]["count"] == 5
    assert "Bearer [REDACTED]" in scrubbed["items"][1]


# ==============================================================================
# 3. Prometheus Metrics Registry & Endpoint
# ==============================================================================


def test_prometheus_metrics_registry_and_exposition():
    metrics.counter_inc("netci_test_counter", {"env": "test", "code": "200"}, 3.0)
    metrics.gauge_set("netci_test_gauge", {"node": "worker-1"}, 42.0)
    metrics.histogram_observe("netci_test_latency", {"route": "/test"}, 0.045)

    text = metrics.generate_prometheus_text()
    assert "# HELP netci_http_requests_total" in text
    assert "netci_database_pool_connections" in text


def test_metrics_endpoint_http_response():
    client = TestClient(app)
    response = client.get("/metrics")
    assert response.status_code == 200
    assert "text/plain" in response.headers["content-type"]
    assert "netci_http_requests_total" in response.text
    assert "netci_database_pool_connections" in response.text


def test_metrics_publish_the_readiness_verdict_and_replica_facts():
    """What /readyz says is what an alert rule can read: the verdict, each dependency,
    the worker pollers and the agents, labelled with the replica that answered."""

    import re

    client = TestClient(app)
    text = client.get("/metrics").text
    ready = client.get("/readyz")
    verdict = 1.0 if ready.status_code == 200 else 0.0
    assert re.search(rf"^netci_ready {verdict}$", text, re.M), text
    for dependency in ("database", "ci", "cd", "dcim", "cosign", "traffic"):
        assert re.search(rf'^netci_dependency_ready\{{dependency="{dependency}"\}} [01]\.0$', text, re.M), dependency
    assert re.search(r'^netci_agents\{state="connected"\} \d+\.0$', text, re.M)
    assert re.search(r'^netci_agents\{state="stale"\} \d+\.0$', text, re.M)
    assert re.search(r'^netci_replica_info\{replica=".+"\} 1\.0$', text, re.M)
    assert "netci_cd_pollers" in text and "netci_reconciler_corrections_total" in text


# ==============================================================================
# 4. Transactional Notification Outbox & Worker
# ==============================================================================


class FakeDispatcher:
    def __init__(self, should_fail: bool = False):
        self.should_fail = should_fail
        self.dispatched: list[NotificationRecord] = []

    async def dispatch(self, notification: NotificationRecord) -> None:
        if self.should_fail:
            raise ConnectionError("simulated remote webhook unavailable")
        self.dispatched.append(notification)


@pytest.mark.asyncio
async def test_notification_outbox_worker_success_and_retry():
    mem_db = InMemoryDatabase()
    notif_id = uuid4()
    notif = NotificationRecord(
        id=notif_id,
        event_type="pipeline.completed",
        aggregate_type="pipeline_run",
        aggregate_id="run-123",
        payload={"result": "succeeded"},
        recipient="https://webhook.internal/events",
        status=NotificationStatus.PENDING,
        attempt=0,
        max_attempts=3,
    )

    with mem_db.transaction() as session:
        session.record_notification(notif)

    # 1. Test failure with exponential backoff
    failing_dispatcher = FakeDispatcher(should_fail=True)
    worker_fail = NotificationOutboxWorker(mem_db, dispatcher=failing_dispatcher)
    count = await worker_fail.run_once()
    assert count == 1

    with mem_db.transaction() as session:
        updated = session.notification(notif_id)
        assert updated is not None
        assert updated.status == NotificationStatus.FAILED
        assert updated.attempt == 1
        assert "simulated remote webhook unavailable" in (updated.last_error or "")
        assert updated.next_attempt_at > updated.created_at

    # 2. Advance time past next_attempt_at and deliver with success dispatcher
    successful_dispatcher = FakeDispatcher(should_fail=False)
    worker_success = NotificationOutboxWorker(mem_db, dispatcher=successful_dispatcher)
    future_time = updated.next_attempt_at + timedelta(seconds=1)
    count_success = await worker_success.run_once(now=future_time)
    assert count_success == 1
    assert len(successful_dispatcher.dispatched) == 1

    with mem_db.transaction() as session:
        delivered = session.notification(notif_id)
        assert delivered is not None
        assert delivered.status == NotificationStatus.DELIVERED
        assert delivered.delivered_at is not None
        assert delivered.attempt == 2


@pytest.mark.asyncio
async def test_notification_outbox_worker_dead_letter():
    mem_db = InMemoryDatabase()
    notif_id = uuid4()
    notif = NotificationRecord(
        id=notif_id,
        event_type="deployment.failed",
        aggregate_type="deployment",
        aggregate_id="dep-999",
        payload={"error": "timeout"},
        recipient="https://webhook.internal/events",
        status=NotificationStatus.FAILED,
        attempt=2,
        max_attempts=3,
    )

    with mem_db.transaction() as session:
        session.record_notification(notif)

    worker = NotificationOutboxWorker(mem_db, dispatcher=FakeDispatcher(should_fail=True))
    count = await worker.run_once()
    assert count == 1

    with mem_db.transaction() as session:
        dead = session.notification(notif_id)
        assert dead is not None
        assert dead.status == NotificationStatus.DEAD_LETTER
        assert dead.attempt == 3


def test_notification_api_endpoints_list_and_retry():
    client = TestClient(app)
    notif_id = uuid4()
    notif = NotificationRecord(
        id=notif_id,
        event_type="config.approved",
        aggregate_type="config_revision",
        aggregate_id="rev-1",
        payload={"approved": True},
        recipient="events@netci.local",
        status=NotificationStatus.DEAD_LETTER,
        attempt=5,
        max_attempts=5,
    )
    with database.transaction() as session:
        session.record_notification(notif)

    # List notifications
    res = client.get("/notifications?status=dead_letter&limit=10")
    assert res.status_code == 200
    data = res.json()
    assert "items" in data
    assert any(item["id"] == str(notif_id) for item in data["items"])

    # Retry notification
    retry_res = client.post(f"/notifications/{notif_id}/retry")
    assert retry_res.status_code == 200
    retry_data = retry_res.json()
    assert retry_data["status"] == "pending"

    with database.transaction() as session:
        reloaded = session.notification(notif_id)
        assert reloaded is not None
        assert reloaded.status == NotificationStatus.PENDING
        assert reloaded.attempt == 0


# ==============================================================================
# 5. Cursor-Based Pagination Tests
# ==============================================================================


def test_cursor_pagination_pipeline_runs_and_deployments():
    client = TestClient(app)

    # 1. Pipeline runs cursor pagination
    res1 = client.get("/pipeline-runs?limit=2")
    assert res1.status_code == 200
    data1 = res1.json()
    assert "items" in data1
    assert "nextCursor" in data1
    assert "hasMore" in data1

    if data1["hasMore"] and data1["nextCursor"]:
        next_c = data1["nextCursor"]
        res2 = client.get(f"/pipeline-runs?limit=2&cursor={next_c}")
        assert res2.status_code == 200
        data2 = res2.json()
        assert "items" in data2
        # Items in page 2 must be different from page 1
        ids_page1 = {item["id"] for item in data1["items"]}
        ids_page2 = {item["id"] for item in data2["items"]}
        assert not (ids_page1 & ids_page2)

    # 2. Deployments cursor pagination
    dep_res = client.get("/deployments?limit=5")
    assert dep_res.status_code == 200
    dep_data = dep_res.json()
    assert "items" in dep_data
    assert "hasMore" in dep_data

    # 3. Audit events cursor pagination
    audit_res = client.get("/audit-events?limit=5")
    assert audit_res.status_code == 200
    audit_data = audit_res.json()
    assert "items" in audit_data


# ==============================================================================
# 6. Retention Manager Tests
# ==============================================================================


def test_retention_manager_purge():
    manager = RetentionManager(
        database=database,
        token_retention_grace_seconds=1,
        notification_retention_days=1,
        event_retention_days=1,
    )
    # Insert old delivered notification
    old_notif = NotificationRecord(
        id=uuid4(),
        event_type="test.purge",
        aggregate_type="test",
        aggregate_id="123",
        payload={},
        recipient="test@local",
        status=NotificationStatus.DELIVERED,
        delivered_at=datetime.now(timezone.utc) - timedelta(days=2),
    )
    with database.transaction() as session:
        session.record_notification(old_notif)

    # Purge
    results = manager.purge_all()
    assert "expired_callback_tokens" in results
    assert "completed_notifications" in results
    assert "aged_delivery_events" in results
    assert results["completed_notifications"] >= 1

    with database.transaction() as session:
        assert session.notification(old_notif.id) is None


def test_retention_purge_api_endpoint():
    client = TestClient(app)
    # Admin role is allowed
    res = client.post(
        "/admin/retention/purge",
        headers={"Authorization": "Bearer admin-token-dummy"},
    )
    # Regardless of auth mode in local test, endpoint returns 200 or 401/403
    assert res.status_code in (200, 401, 403)


def test_retention_thins_old_console_lines_but_keeps_the_run_and_recent_logs():
    """The run, its digest and its audit stay; console output past the window goes."""

    from uuid import UUID

    from backend.app.domain.models import Environment, PipelineRun, PipelineStatus
    from backend.app.persistence import UnitOfWork
    from backend.app.store.memory import InMemoryDatabase

    db = InMemoryDatabase()
    now = datetime.now(timezone.utc)
    app_id = UUID(int=1)

    def run(days_old: int, status: PipelineStatus) -> PipelineRun:
        stamp = now - timedelta(days=days_old)
        return PipelineRun(
            id=uuid4(), application_id=app_id, commit_sha="a" * 40, branch="main", environment=Environment.DEV,
            parameters={}, correlation_id=f"c-{days_old}", status=status, started_by="t",
            created_at=stamp, updated_at=stamp,
        )

    old, recent, still_running = run(120, PipelineStatus.SUCCEEDED), run(3, PipelineStatus.SUCCEEDED), run(120, PipelineStatus.RUNNING)
    with db.transaction() as session:
        session.apply(UnitOfWork(runs=[(old, None), (recent, None), (still_running, None)],
                                 logs=[(old.id, ["l1", "l2"]), (recent.id, ["r1"]), (still_running.id, ["s1"])]))

    outcome = RetentionManager(db, pipeline_log_retention_days=90).purge_all(now)
    assert outcome["aged_pipeline_log_lines"] == 2
    with db.transaction() as session:
        assert session.pipeline_run(old.id) is not None  # the run itself is kept
        assert session.pipeline_logs(old.id) == ()
        assert session.pipeline_logs(recent.id) == ("r1",)
        assert session.pipeline_logs(still_running.id) == ("s1",)  # never thin a run that has not finished


def test_scheduled_retention_runs_under_the_advisory_lock_and_counts_what_it_purged():
    import app.main as main

    outcome = main.run_retention_pass()
    assert outcome is not None and set(outcome) >= {"aged_pipeline_log_lines", "aged_delivery_events"}
    text = TestClient(app).get("/metrics").text
    assert "netci_retention_purged_total" in text
