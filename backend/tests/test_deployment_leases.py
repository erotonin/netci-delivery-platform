"""Deployment exclusion, fencing and log-append safety.

Three defects these cover. Two approvals seconds apart used to produce two workflows
writing to the same hosts, and the surviving state was whichever finished last. A workflow
that timed out and came back could overwrite the result of the workflow that replaced it.
And two concurrent log appends computed the same `max(sequence) + 1`, so one died on the
primary key and took its whole unit of work with it.

Runs against a real PostgreSQL, because every one of these is a claim about what the
database refuses, not about what one process remembers.
"""

from __future__ import annotations

import os
import threading
import uuid

import pytest

from app.delivery import DeliveryError, DeliveryPlatform
from app.domain.models import DeploymentStatus, Environment, PipelineStatus, Runtime
from app.store import PostgresDatabase

DATABASE_URL = os.getenv("NETCI_TEST_DATABASE_URL", "").strip()

pytestmark = pytest.mark.skipif(
    not DATABASE_URL, reason="set NETCI_TEST_DATABASE_URL to a migrated PostgreSQL"
)

DIGEST = "sha256:" + "c" * 64
OLDER_DIGEST = "sha256:" + "d" * 64


@pytest.fixture()
def database(monkeypatch):
    import psycopg

    monkeypatch.setenv("DATABASE_URL", DATABASE_URL)

    def wipe():
        with psycopg.connect(DATABASE_URL) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "TRUNCATE deployment_leases, deployment_fencing_counters,"
                    " callback_token_uses, pipeline_log_sequences, security_evidence,"
                    " delivery_events, pipeline_logs, audit_events, idempotency_records,"
                    " deployments, pipeline_runs, applications RESTART IDENTITY CASCADE"
                )

    wipe()
    yield DATABASE_URL
    wipe()


def platform() -> DeliveryPlatform:
    return DeliveryPlatform(database=PostgresDatabase(DATABASE_URL))


def evidence(item) -> dict:
    return {
        "artifactDigest": DIGEST,
        "sbom": {"generatedBy": "syft", "location": "s3://evidence/sbom.json"},
        "vulnerabilityScan": {"scanner": "trivy", "status": "passed", "critical": 0, "high": 0},
        "signature": {"provider": "cosign", "verified": True},
    }


def new_application(name: str | None = None, engine=None):
    engine = engine or platform()
    name = name or f"lease-{uuid.uuid4().hex[:8]}"
    return engine, engine.create_application(
        name=name,
        repository_url=f"https://github.com/example/{name}",
        pipeline_template="container-ci-cd-v1",
        runtime=Runtime.DOCKER,
        default_environment=Environment.DEV,
        stages=[],
        idempotency_key=None,
    )


def release(engine, application, target_hosts: list[str], commit: str = "abc1234"):
    """One production release of an application, waiting for approval.

    `target_hosts` is server-managed -- the Portal computes it from the module's
    registered configuration -- so setting it here is what onboarding would have done.
    """

    run = engine.start_pipeline(
        application.id,
        commit_sha=commit,
        branch="main",
        environment=Environment.PROD,
        parameters={"target_hosts": target_hosts},
        correlation_id="lease-test",
        idempotency_key=None,
    )
    engine.record_ci_result(run.id, PipelineStatus.RUNNING.value, None, [])
    engine.record_security_evidence(run.id, evidence(run))
    deployment = engine.record_ci_result(
        run.id, PipelineStatus.SUCCEEDED.value, DIGEST, []
    ).deployment
    assert deployment is not None
    return run, deployment


def deployment_on(target_hosts: list[str], name: str | None = None, engine=None):
    """One application with one production release pending approval."""

    engine, application = new_application(name, engine)
    run, deployment = release(engine, application, target_hosts)
    return engine, application, run, deployment


def two_releases_of_one_application(target_hosts: list[str], name: str):
    """The real collision: two releases of the same service racing for its hosts."""

    engine, application = new_application(name)
    _, first = release(engine, application, target_hosts, commit="aaa1111")
    _, second = release(engine, application, target_hosts, commit="bbb2222")
    return engine, application, first, second


# ------------------------------------------------------------------- exclusion


def test_two_concurrent_approvals_for_one_target_leave_exactly_one_deploying(database):
    """The database decides, because the two requests may reach different replicas."""

    hosts = ["prod-a", "prod-b"]
    _, _, first, second = two_releases_of_one_application(hosts, "lease-race")

    started = threading.Barrier(2)
    outcomes: list[str] = []
    lock = threading.Lock()

    def approve(deployment_id, actor):
        engine = platform()
        started.wait(timeout=10)
        try:
            engine.approve_deployment(deployment_id, actor)
            result = "deploying"
        except DeliveryError as exc:
            result = exc.code
        with lock:
            outcomes.append(result)

    threads = [
        threading.Thread(target=approve, args=(first.id, "reviewer-1")),
        threading.Thread(target=approve, args=(second.id, "reviewer-2")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert outcomes.count("deploying") == 1, outcomes
    assert outcomes.count("DEPLOYMENT_TARGET_BUSY") == 1, outcomes
    engine = platform()
    statuses = {
        engine.get_deployment(first.id).status,
        engine.get_deployment(second.id).status,
    }
    assert DeploymentStatus.DEPLOYING in statuses
    assert DeploymentStatus.PENDING_APPROVAL in statuses


def test_two_releases_to_different_targets_do_not_collide(database):
    """The lock is over the target, not over the application.

    One service with a blue and a green host set can deploy to both at once; the same
    service twice to the same hosts cannot.
    """

    engine, application = new_application("lease-targets")
    _, blue = release(engine, application, ["prod-blue"], commit="aaa1111")
    _, green = release(engine, application, ["prod-green"], commit="bbb2222")

    engine.approve_deployment(blue.id, "reviewer-1")
    engine.approve_deployment(green.id, "reviewer-2")

    assert engine.get_deployment(blue.id).status == DeploymentStatus.DEPLOYING
    assert engine.get_deployment(green.id).status == DeploymentStatus.DEPLOYING


def test_two_applications_sharing_a_host_are_not_treated_as_a_collision(database):
    """Two different services on one host are two services, not one resource."""

    engine_a, application_a = new_application("share-a")
    engine_b, application_b = new_application("share-b")
    _, first = release(engine_a, application_a, ["shared-host"])
    _, second = release(engine_b, application_b, ["shared-host"])

    engine_a.approve_deployment(first.id, "reviewer-1")
    engine_b.approve_deployment(second.id, "reviewer-2")

    assert engine_a.get_deployment(first.id).status == DeploymentStatus.DEPLOYING
    assert engine_b.get_deployment(second.id).status == DeploymentStatus.DEPLOYING


def test_the_target_is_free_again_once_the_deployment_reaches_a_terminal_state(database):
    hosts = ["prod-shared"]
    engine, _, first, second = two_releases_of_one_application(hosts, "lease-free")
    engine.approve_deployment(first.id, "reviewer-1")

    with pytest.raises(DeliveryError) as blocked:
        platform().approve_deployment(second.id, "reviewer-2")
    assert blocked.value.code == "DEPLOYMENT_TARGET_BUSY"

    engine.record_deployment_result(first.id, DeploymentStatus.HEALTHY.value, "ok")

    platform().approve_deployment(second.id, "reviewer-2")
    assert platform().get_deployment(second.id).status == DeploymentStatus.DEPLOYING


def test_an_expired_lease_is_reclaimed_by_the_next_deployment(database, monkeypatch):
    """A worker that died must not block its target until someone notices."""

    monkeypatch.setenv("NETCI_DEPLOYMENT_LEASE_TTL_SECONDS", "30")
    hosts = ["prod-expiring"]
    engine, _, first, second = two_releases_of_one_application(hosts, "lease-expire")
    engine.approve_deployment(first.id, "reviewer-1")

    # The holder stops heartbeating: age its lease past its expiry.
    import psycopg

    with psycopg.connect(DATABASE_URL) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE deployment_leases SET expires_at = now() - interval '1 minute'"
                " WHERE deployment_id = %s",
                (first.id,),
            )

    platform().approve_deployment(second.id, "reviewer-2")

    assert platform().get_deployment(second.id).status == DeploymentStatus.DEPLOYING


# --------------------------------------------------------------------- fencing


def test_a_superseded_workflow_cannot_report_a_result(database, monkeypatch):
    """The whole point of the fencing token: an older writer must be refused."""

    monkeypatch.setenv("NETCI_DEPLOYMENT_LEASE_TTL_SECONDS", "30")
    hosts = ["prod-fenced"]
    engine, _, first, second = two_releases_of_one_application(hosts, "fence")
    engine.approve_deployment(first.id, "reviewer-1")
    stale_token = platform().get_deployment(first.id).fencing_token
    assert stale_token is not None

    # The first workflow goes silent, its lease expires, and a newer one takes the target.
    import psycopg

    with psycopg.connect(DATABASE_URL) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE deployment_leases SET expires_at = now() - interval '1 minute'"
                " WHERE deployment_id = %s",
                (first.id,),
            )
    platform().approve_deployment(second.id, "reviewer-2")
    fresh_token = platform().get_deployment(second.id).fencing_token
    assert fresh_token > stale_token

    # Now the first workflow wakes up and tries to report.
    with pytest.raises(DeliveryError) as failure:
        platform().record_deployment_result(
            first.id, DeploymentStatus.HEALTHY.value, "late", fencing_token=stale_token
        )

    assert failure.value.code == "STALE_WORKFLOW"
    assert failure.value.status_code == 409
    assert platform().get_deployment(first.id).status == DeploymentStatus.DEPLOYING


def test_a_tokenless_stale_writer_is_refused_too(database, monkeypatch):
    """A worker that predates fencing is checked by asking whether the target moved on."""

    monkeypatch.setenv("NETCI_DEPLOYMENT_LEASE_TTL_SECONDS", "30")
    hosts = ["prod-tokenless"]
    engine, _, first, second = two_releases_of_one_application(hosts, "tokenless")
    engine.approve_deployment(first.id, "reviewer-1")

    import psycopg

    with psycopg.connect(DATABASE_URL) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE deployment_leases SET expires_at = now() - interval '1 minute'"
                " WHERE deployment_id = %s",
                (first.id,),
            )
    platform().approve_deployment(second.id, "reviewer-2")

    with pytest.raises(DeliveryError) as failure:
        platform().record_deployment_result(first.id, DeploymentStatus.HEALTHY.value, "late")

    assert failure.value.code == "STALE_WORKFLOW"


def test_the_current_workflow_reports_normally_with_its_token(database):
    engine, _, _, deployment = deployment_on(["prod-current"], "fence-current")
    engine.approve_deployment(deployment.id, "reviewer-1")
    token = platform().get_deployment(deployment.id).fencing_token

    reported = platform().record_deployment_result(
        deployment.id, DeploymentStatus.HEALTHY.value, "ok", fencing_token=token
    )

    assert reported.status == DeploymentStatus.HEALTHY


def test_a_repeated_terminal_callback_is_idempotent_and_adds_no_duplicate_event(database):
    """Temporal retries. A retry must not double-count a deployment in DORA."""

    engine, application, _, deployment = deployment_on(["prod-retry"], "fence-retry")
    engine.approve_deployment(deployment.id, "reviewer-1")
    token = platform().get_deployment(deployment.id).fencing_token

    for _ in range(3):
        platform().record_deployment_result(
            deployment.id, DeploymentStatus.HEALTHY.value, "ok", fencing_token=token
        )

    events = platform().delivery_events(application.id)
    deployment_events = [item for item in events if item.deployment_id == deployment.id]
    assert len(deployment_events) == 1


def test_a_heartbeat_keeps_the_lease_and_is_refused_once_superseded(database, monkeypatch):
    monkeypatch.setenv("NETCI_DEPLOYMENT_LEASE_TTL_SECONDS", "30")
    engine, _, _, deployment = deployment_on(["prod-heartbeat"], "hb-one")
    engine.approve_deployment(deployment.id, "reviewer-1")
    token = platform().get_deployment(deployment.id).fencing_token

    extended = platform().heartbeat_deployment(deployment.id, token)
    assert extended["fencingToken"] == token

    with pytest.raises(DeliveryError) as failure:
        platform().heartbeat_deployment(deployment.id, token - 1 if token > 1 else 0)
    assert failure.value.code == "STALE_WORKFLOW"


# ------------------------------------------------------------- rollback states


def test_a_failed_rollback_has_its_own_state_and_emits_no_recovery_event(database):
    """Calling it `failed` would lose that recovery was attempted and did not work."""

    engine, application, _, deployment = deployment_on(["prod-rollback"], "rb-fail")
    engine.approve_deployment(deployment.id, "reviewer-1")
    token = platform().get_deployment(deployment.id).fencing_token
    platform().record_deployment_result(
        deployment.id, DeploymentStatus.FAILED.value, "boom", fencing_token=token
    )
    rolled = platform().rollback_deployment(deployment.id, OLDER_DIGEST)

    failed = platform().record_rollback_result(
        rolled.id, succeeded=False, message="ansible could not reach the host"
    )

    assert failed.status == DeploymentStatus.ROLLBACK_FAILED
    recoveries = [
        item
        for item in platform().delivery_events(application.id)
        if item.event_type.value == "recovery"
    ]
    assert recoveries == [], "a rollback that failed restored nothing"


def test_a_successful_rollback_records_the_restore(database):
    engine, application, _, deployment = deployment_on(["prod-restore"], "rb-ok")
    engine.approve_deployment(deployment.id, "reviewer-1")
    token = platform().get_deployment(deployment.id).fencing_token
    platform().record_deployment_result(
        deployment.id, DeploymentStatus.FAILED.value, "boom", fencing_token=token
    )
    rolled = platform().rollback_deployment(deployment.id, OLDER_DIGEST)

    restored = platform().record_rollback_result(rolled.id, succeeded=True, message="back")

    assert restored.status == DeploymentStatus.ROLLED_BACK
    recoveries = [
        item
        for item in platform().delivery_events(application.id)
        if item.event_type.value == "recovery"
    ]
    assert len(recoveries) >= 1
    assert recoveries[-1].successful is True


# ------------------------------------------------------------------- log safety


def test_concurrent_log_appends_keep_ordering_and_never_collide(database):
    """`max(sequence) + 1` under concurrency loses a whole unit of work to a PK clash."""

    engine, application, run, _ = deployment_on(["prod-logs"], "log-race")
    writers = 8
    per_writer = 25
    started = threading.Barrier(writers)
    failures: list[str] = []
    lock = threading.Lock()

    def append(index: int) -> None:
        worker = platform()
        started.wait(timeout=15)
        try:
            for line in range(per_writer):
                worker.append_pipeline_logs(run.id, [f"writer={index} line={line}"])
        except Exception as exc:  # noqa: BLE001 - the failure mode is the assertion
            with lock:
                failures.append(f"{type(exc).__name__}: {exc}")

    threads = [threading.Thread(target=append, args=(index,)) for index in range(writers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert failures == [], failures
    _, lines = platform().get_pipeline_logs(run.id)
    written = [line for line in lines if line.startswith("writer=")]
    assert len(written) == writers * per_writer

    import psycopg

    with psycopg.connect(DATABASE_URL) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) AS total, count(DISTINCT sequence) AS distinct_sequences"
                " FROM pipeline_logs WHERE pipeline_run_id = %s",
                (run.id,),
            )
            total, distinct = cursor.fetchone()
    assert total == distinct, "a sequence number was handed out twice"

    # Each writer's own lines stay in the order it wrote them.
    for index in range(writers):
        mine = [line for line in written if line.startswith(f"writer={index} ")]
        assert mine == [f"writer={index} line={line}" for line in range(per_writer)]


# --------------------------------------------------------------------- recovery


def test_expired_leases_are_reported_so_a_dead_worker_is_visible(database, monkeypatch):
    monkeypatch.setenv("NETCI_DEPLOYMENT_LEASE_TTL_SECONDS", "30")
    engine, _, _, deployment = deployment_on(["prod-dead"], "recover-one")
    engine.approve_deployment(deployment.id, "reviewer-1")

    import psycopg

    with psycopg.connect(DATABASE_URL) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE deployment_leases SET expires_at = now() - interval '1 minute'"
                " WHERE deployment_id = %s",
                (deployment.id,),
            )

    recovered = platform().recover_expired_leases()

    assert [item["deploymentId"] for item in recovered] == [str(deployment.id)]
    kinds = {record.event_type for record in platform().audit_records()}
    assert "deployment.lease_expired" in kinds
    # Recovering twice reports nothing the second time.
    assert platform().recover_expired_leases() == []


def test_every_lease_decision_is_in_the_audit_trail(database):
    hosts = ["prod-audit"]
    engine, _, first, second = two_releases_of_one_application(hosts, "audit")
    engine.approve_deployment(first.id, "reviewer-1")
    with pytest.raises(DeliveryError):
        platform().approve_deployment(second.id, "reviewer-2")
    token = platform().get_deployment(first.id).fencing_token
    platform().record_deployment_result(
        first.id, DeploymentStatus.HEALTHY.value, "ok", fencing_token=token
    )

    kinds = {record.event_type for record in platform().audit_records()}

    assert "deployment.lease_acquired" in kinds
    assert "deployment.lease_conflict" in kinds
    assert "deployment.lease_released" in kinds


# ------------------------------------------------------------ redeploy on PostgreSQL


def test_a_redeploy_writes_the_deployment_before_its_lease(database):
    """Found live: `redeploy_artifact` acquired the lease before inserting the deployment
    row, and PostgreSQL refused the lease's foreign key. The in-memory store enforces no
    constraint, so only a PostgreSQL-backed test can hold this."""

    engine, application = new_application("redeploy-fk")
    run = engine.start_pipeline(
        application.id, commit_sha="abc1234", branch="main", environment=Environment.DEV,
        parameters={"target_hosts": ["dev-01"]}, correlation_id="lease-test", idempotency_key=None,
    )
    engine.record_ci_result(run.id, PipelineStatus.RUNNING.value, None, [])
    engine.record_security_evidence(run.id, evidence(run))
    first = engine.record_ci_result(run.id, PipelineStatus.SUCCEEDED.value, DIGEST, []).deployment
    engine.record_deployment_result(first.id, DeploymentStatus.HEALTHY.value, "ok", fencing_token=platform().get_deployment(first.id).fencing_token)

    source = engine.source_run_in_service(application.id, Environment.DEV)
    assert source is not None and source.id == run.id
    redeployed = engine.redeploy_artifact(
        application.id, environment=Environment.DEV, source_pipeline_run_id=source.id,
        config_revision_id=None, actor="ops", parameters={"target_hosts": ["dev-01"]}, reason="config",
    )
    assert redeployed.status == DeploymentStatus.DEPLOYING
    assert redeployed.fencing_token is not None
    stored = platform().get_deployment(redeployed.id)
    assert stored.fencing_token == redeployed.fencing_token
    assert stored.previous_artifact_digest == DIGEST
