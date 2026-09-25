"""Tests for the ephemeral preview environment domain (ADR-049).

A preview is a deployment: `request()` never writes "active" -- only `record_result`, the
worker's own report, can. These tests exercise `PreviewEnvironmentManager` directly, the
same seam the Portal and the API callback both drive.
"""

from datetime import datetime, timedelta, timezone
from uuid import uuid4
import pytest
from app.catalog.previews import PreviewEnvironmentError, PreviewEnvironmentManager
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
            runtime=Runtime.KUBERNETES,
            default_environment=Environment.DEV,
            stages=(),
            owner_team="checkout-team",
            created_at=datetime.now(timezone.utc),
        )
        session._state.applications[app_id] = app
        yield PreviewEnvironmentManager(session), session, app_id


def _request(mgr, app_id, *, module_id="checkout", pr_number=123, run_id=None, digest=None, ttl_hours=2):
    return mgr.request(
        application_id=app_id,
        module_id=module_id,
        pull_request_id=str(pr_number),
        pull_request_number=pr_number,
        pipeline_run_id=run_id or uuid4(),
        commit_sha="c" * 40,
        artifact_digest=digest or f"sha256:{'a' * 64}",
        ttl_hours=ttl_hours,
        created_by="developer-alice",
    )


def test_request_writes_a_deploying_row_never_active(preview_setup):
    mgr, session, app_id = preview_setup

    prv = _request(mgr, app_id)

    assert prv.status == "deploying"
    assert prv.url is None
    assert prv.application_id == app_id
    assert prv.namespace == "preview-checkout-pr-123"
    assert prv.release_name == "checkout-pr-123"
    assert prv.id == "checkout-pr-123"
    assert prv.pipeline_run_id is not None
    assert prv.artifact_digest is not None


def test_a_second_push_redeploys_the_same_row(preview_setup):
    mgr, session, app_id = preview_setup
    first = _request(mgr, app_id)
    second_run = uuid4()
    second_digest = f"sha256:{'b' * 64}"

    second = _request(mgr, app_id, run_id=second_run, digest=second_digest)

    assert second.id == first.id
    assert second.namespace == first.namespace
    assert second.pipeline_run_id == second_run
    assert second.artifact_digest == second_digest
    assert session.list_preview_environments(application_id=app_id) == (second,)


def test_record_result_active_requires_the_worker_and_carries_its_url(preview_setup):
    mgr, session, app_id = preview_setup
    prv = _request(mgr, app_id)

    updated = mgr.record_result(prv.id, status="active", message="deployed", url="https://checkout-pr-123.preview.local")

    assert updated.status == "active"
    assert updated.url == "https://checkout-pr-123.preview.local"
    assert updated.detail == "deployed"


def test_record_result_active_without_a_url_keeps_it_null(preview_setup):
    mgr, session, app_id = preview_setup
    prv = _request(mgr, app_id)

    updated = mgr.record_result(prv.id, status="active", message="no ingress")

    assert updated.status == "active"
    assert updated.url is None


def test_record_result_refuses_a_transition_the_table_does_not_allow(preview_setup):
    mgr, session, app_id = preview_setup
    prv = _request(mgr, app_id)
    mgr.record_result(prv.id, status="active", url="https://x.example")

    with pytest.raises(PreviewEnvironmentError) as excinfo:
        mgr.record_result(prv.id, status="active", url="https://replayed.example")

    assert excinfo.value.code == "INVALID_PREVIEW_STATE"
    assert excinfo.value.status_code == 409
    # Untouched by the refused call.
    assert session.preview_environment(prv.id).url == "https://x.example"


def test_record_result_for_an_unknown_preview_is_404(preview_setup):
    mgr, _, _ = preview_setup

    with pytest.raises(PreviewEnvironmentError) as excinfo:
        mgr.record_result("no-such-preview", status="active")

    assert excinfo.value.status_code == 404


def test_start_teardown_moves_active_or_deploying_to_destroying(preview_setup):
    mgr, session, app_id = preview_setup
    prv = _request(mgr, app_id)
    mgr.record_result(prv.id, status="active", url="https://x.example")

    destroying = mgr.start_teardown(prv.id, detail="teardown requested by alice")

    assert destroying.status == "destroying"
    assert destroying.detail == "teardown requested by alice"


def test_a_failed_preview_is_still_torn_down_because_its_namespace_may_exist(preview_setup):
    # A deploy can fail after the playbook created the namespace. Left at "failed" with
    # no path to teardown, that namespace would outlive the pull request forever.
    mgr, session, app_id = preview_setup
    prv = _request(mgr, app_id)
    mgr.record_result(prv.id, status="failed", message="helm upgrade timed out")

    torn = mgr.start_teardown(prv.id, detail="pull request closed")
    assert torn is not None and torn.status == "destroying"


def test_start_teardown_is_none_for_a_row_already_gone(preview_setup):
    mgr, session, app_id = preview_setup
    prv = _request(mgr, app_id)
    mgr.start_teardown(prv.id, detail="teardown requested")
    mgr.record_result(prv.id, status="destroyed")

    assert mgr.start_teardown(prv.id, detail="again") is None
    assert mgr.start_teardown("does-not-exist", detail="again") is None


def test_teardown_reports_destroyed(preview_setup):
    mgr, session, app_id = preview_setup
    prv = _request(mgr, app_id)
    mgr.record_result(prv.id, status="active", url="https://x.example")
    mgr.start_teardown(prv.id, detail="teardown requested")

    destroyed = mgr.record_result(prv.id, status="destroyed", message="namespace removed")

    assert destroyed.status == "destroyed"
    assert destroyed.destroyed_at is not None


def test_ttl_is_clamped_to_1_and_72_hours(preview_setup):
    mgr, session, app_id = preview_setup
    now = datetime.now(timezone.utc)

    too_short = _request(mgr, app_id, pr_number=1, ttl_hours=0)
    too_long = _request(mgr, app_id, pr_number=2, ttl_hours=1000)

    assert too_short.ttl_seconds == 3600
    assert too_long.ttl_seconds == 72 * 3600
    assert too_short.expires_at <= now + timedelta(hours=1, minutes=1)


def test_reconcile_expiry_moves_expired_rows_to_destroying_and_expiry_is_reported_as_expired(preview_setup):
    mgr, session, app_id = preview_setup
    prv = _request(mgr, app_id, pr_number=7, ttl_hours=1)
    mgr.record_result(prv.id, status="active", url="https://x.example")
    future = datetime.now(timezone.utc) + timedelta(hours=2)

    reconciled = mgr.reconcile_expiry(now=future)

    assert len(reconciled) == 1
    assert reconciled[0].id == prv.id
    assert reconciled[0].status == "destroying"
    assert reconciled[0].detail.startswith("ttl expired")

    # The worker's teardown result for an expiry-started row lands as "expired", not
    # "destroyed" -- there is no other place that fact is recorded.
    final = mgr.record_result(prv.id, status="destroyed", message="namespace removed")
    assert final.status == "expired"
    assert final.destroyed_at is not None


def test_reconcile_expiry_ignores_rows_not_yet_due(preview_setup):
    mgr, session, app_id = preview_setup
    _request(mgr, app_id, pr_number=8, ttl_hours=72)

    assert mgr.reconcile_expiry(now=datetime.now(timezone.utc)) == []


def test_a_preview_being_torn_down_is_not_redeployed_into(preview_setup):
    # The teardown is deleting that namespace; a deploy racing it would leave whichever
    # report arrived second describing a namespace that is not there.
    mgr, session, app_id = preview_setup
    prv = _request(mgr, app_id)
    mgr.start_teardown(prv.id, detail="pull request closed")

    with pytest.raises(PreviewEnvironmentError) as refused:
        _request(mgr, app_id)
    assert refused.value.code == "PREVIEW_TEARING_DOWN" and refused.value.status_code == 409
    assert session.preview_environment(prv.id).status == "destroying"


def test_a_savepoint_undoes_only_its_own_writes_in_the_memory_store_too(preview_setup):
    mgr, session, app_id = preview_setup
    kept = _request(mgr, app_id, pr_number=1)
    with pytest.raises(RuntimeError):
        with session.savepoint():
            _request(mgr, app_id, pr_number=2)
            raise RuntimeError("the preview failed")
    assert session.preview_environment(kept.id) is not None
    assert [p.id for p in session.list_preview_environments(app_id)] == [kept.id]
