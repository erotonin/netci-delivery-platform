"""Tests for Ephemeral Preview Environments."""

from datetime import datetime, timedelta, timezone
from uuid import uuid4
import pytest
from app.catalog.previews import PreviewEnvironmentManager, PreviewEnvironmentError
from app.domain.models import Application, Environment, Runtime
from app.store.memory import InMemoryDatabase


@pytest.fixture()
def preview_setup():
    db = InMemoryDatabase()
    with db.transaction() as session:
        app_id = uuid4()
        app = Application(
            id=app_id,
            name="checkout-service",
            repository_url="https://github.com/org/checkout",
            pipeline_template="fastapi-service",
            runtime=Runtime.DOCKER,
            default_environment=Environment.DEV,
            stages=(),
            owner_team="checkout-team",
            created_at=datetime.now(timezone.utc),
        )
        session._state.applications[app_id] = app
        yield PreviewEnvironmentManager(session), session, app_id


def test_create_and_teardown_preview(preview_setup):
    mgr, session, app_id = preview_setup
    prv = mgr.create_preview(
        application_id=app_id,
        pull_request_id="PR-123",
        commit_sha="c" * 40,
        ttl_seconds=7200,
        created_by="developer-alice",
    )
    assert prv.status == "active"
    assert prv.application_id == app_id
    assert prv.pull_request_id == "PR-123"
    assert prv.ttl_seconds == 7200
    assert "pr-123" in prv.id
    assert "pr-123" in prv.namespace
    assert prv.url == f"https://{prv.id}.preview.netci.internal"

    # Teardown
    destroyed = mgr.teardown_preview(prv.id)
    assert destroyed.status == "destroyed"
    assert destroyed.destroyed_at is not None


def test_ttl_bounds_and_expiry_reconciliation(preview_setup):
    mgr, session, app_id = preview_setup
    now = datetime.now(timezone.utc)

    # Below min TTL clamped to MIN_TTL (3600s)
    prv_short = mgr.create_preview(
        application_id=app_id,
        pull_request_id="PR-999",
        commit_sha="d" * 40,
        ttl_seconds=300,  # 5 min -> clamped to 3600
    )
    assert prv_short.ttl_seconds == 3600

    # Simulate time elapsed
    future_time = now + timedelta(seconds=3601)
    expired = mgr.reconcile_expiry(now=future_time)
    assert any(p.id == prv_short.id for p in expired)

    re_fetched = session.preview_environment(prv_short.id)
    assert re_fetched.status == "expired"
