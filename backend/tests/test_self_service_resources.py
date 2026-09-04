"""Tests for Self-Service Resource Requests with fail-closed provider contract and dual control."""

from datetime import datetime, timezone
from uuid import uuid4
import pytest
from app.catalog.resources import SelfServiceResourceManager, ResourceRequestError
from app.domain.models import Application, Environment, Runtime
from app.store.memory import InMemoryDatabase


@pytest.fixture()
def resource_setup():
    db = InMemoryDatabase()
    with db.transaction() as session:
        app_id = uuid4()
        app = Application(
            id=app_id,
            name="cart-service",
            repository_url="https://github.com/org/cart",
            pipeline_template="fastapi-service",
            runtime=Runtime.DOCKER,
            default_environment=Environment.DEV,
            stages=(),
            owner_team="cart-team",
            created_at=datetime.now(timezone.utc),
        )
        session._state.applications[app_id] = app
        yield session, app_id


def test_unconfigured_provider_fails_closed(resource_setup):
    session, app_id = resource_setup
    mgr = SelfServiceResourceManager(session, provider="unconfigured")

    # In preview env with unconfigured provider, status is provider_not_configured (zero fake success)
    req = mgr.request_resource(
        application_id=app_id,
        team_id="cart-team",
        environment="preview",
        resource_type="postgres_database",
        spec={"size_gb": 5},
        requested_by="alice",
    )
    assert req.status == "provider_not_configured"
    assert "No infrastructure provider" in req.status_reason
    assert req.outputs == {}


def test_configured_provider_provisions_preview_resource(resource_setup):
    session, app_id = resource_setup
    mgr = SelfServiceResourceManager(session, provider="terraform")

    req = mgr.request_resource(
        application_id=app_id,
        team_id="cart-team",
        environment="preview",
        resource_type="redis_cache",
        spec={},
        requested_by="alice",
    )
    assert req.status == "ready"
    assert req.provider == "terraform"
    assert req.outputs.get("port") == 6379
    assert "vault://" in req.outputs.get("secret_reference")


def test_staging_production_requires_approval_and_enforces_dual_control(resource_setup):
    session, app_id = resource_setup
    mgr = SelfServiceResourceManager(session, provider="terraform")

    req = mgr.request_resource(
        application_id=app_id,
        team_id="cart-team",
        environment="production",
        resource_type="s3_bucket",
        spec={"bucket_name": "cart-prod-data"},
        requested_by="alice",
    )
    assert req.status == "pending_approval"
    assert req.outputs == {}

    # Self-approval must fail with separation of duties
    with pytest.raises(ResourceRequestError, match="Separation of duties violation"):
        mgr.approve_resource(req.id, approved_by="alice")

    # Independent approval succeeds
    approved = mgr.approve_resource(req.id, approved_by="bob-lead")
    assert approved.status == "ready"
    assert approved.approved_by == "bob-lead"
    assert "cart-prod-data" in approved.outputs.get("bucket_name")


def test_deprovision_resource(resource_setup):
    session, app_id = resource_setup
    mgr = SelfServiceResourceManager(session, provider="terraform")

    req = mgr.request_resource(
        application_id=app_id,
        team_id="cart-team",
        environment="development",
        resource_type="kafka_topic",
        requested_by="alice",
    )
    assert req.status == "ready"

    deprovisioned = mgr.deprovision_resource(req.id)
    assert deprovisioned.status == "deprovisioned"
