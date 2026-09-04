"""Durability tests that require a real PostgreSQL.

These are the assertions a unit test cannot make: that state, source events, logs and
idempotency records survive a process restart, and that two processes racing the same
transition cannot both win. Skipped unless NETCI_TEST_DATABASE_URL points at a database
that has had `python scripts/migrate.py` applied.

    docker run -d --name netci-test-pg --network host \\
        -e POSTGRES_DB=netci -e POSTGRES_USER=netci \\
        -e POSTGRES_PASSWORD=netci-local-only -e PGPORT=55432 postgres:16.15-alpine3.24
    export NETCI_TEST_DATABASE_URL=postgresql://netci:netci-local-only@127.0.0.1:55432/netci
    DATABASE_URL=$NETCI_TEST_DATABASE_URL python scripts/migrate.py
    python -m pytest backend/tests/test_persistence_postgres.py -v
"""

from __future__ import annotations

import os
import uuid

import pytest

from app.delivery import DeliveryError, DeliveryPlatform
from app.domain.models import DeliveryEventType, DeploymentStatus, Environment, PipelineStatus, Runtime

DATABASE_URL = os.getenv("NETCI_TEST_DATABASE_URL", "").strip()

pytestmark = pytest.mark.skipif(
    not DATABASE_URL,
    reason="set NETCI_TEST_DATABASE_URL to a migrated PostgreSQL to run durability tests",
)

DIGEST = "sha256:" + "e" * 64
ROLLBACK_DIGEST = "sha256:" + "f" * 64


@pytest.fixture()
def database(monkeypatch):
    """Point the platform at the test database and leave it clean afterwards."""

    monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
    truncate()
    yield DATABASE_URL
    truncate()


def truncate() -> None:
    import psycopg

    with psycopg.connect(DATABASE_URL) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "TRUNCATE security_evidence, delivery_events, pipeline_logs, audit_events, idempotency_records,"
                " deployments, pipeline_runs, applications RESTART IDENTITY CASCADE"
            )


def unique_name() -> str:
    return f"durable-{uuid.uuid4().hex[:8]}"


def seed(platform: DeliveryPlatform, name: str, environment=Environment.PROD):
    application = platform.create_application(
        name=name,
        repository_url=f"https://github.com/example/{name}",
        pipeline_template="container-ci-cd-v1",
        runtime=Runtime.DOCKER,
        default_environment=Environment.DEV,
        stages=[],
        idempotency_key=f"create-{name}",
    )
    run = platform.start_pipeline(
        application.id,
        commit_sha="abc1234",
        branch="main",
        environment=environment,
        parameters={},
        correlation_id="correlation-durable",
        idempotency_key=f"start-{name}",
    )
    return application, run


def test_a_restart_recovers_applications_runs_deployments_and_logs(database):
    platform = DeliveryPlatform()
    application, run = seed(platform, unique_name())
    platform.record_ci_result(run.id, PipelineStatus.RUNNING.value, None, ["stage=build"])
    deployment = platform.record_ci_result(run.id, PipelineStatus.SUCCEEDED.value, DIGEST, ["stage=publish"]).deployment
    assert deployment is not None
    platform.approve_deployment(deployment.id, "reviewer-1")
    platform.record_deployment_result(deployment.id, DeploymentStatus.HEALTHY.value, "healthy in prod")

    restarted = DeliveryPlatform()

    recovered_run = restarted.get_pipeline(run.id)
    assert recovered_run.status == PipelineStatus.SUCCEEDED
    assert recovered_run.artifact_digest == DIGEST
    recovered_deployment = restarted.get_deployment(deployment.id)
    assert recovered_deployment.status == DeploymentStatus.HEALTHY
    assert recovered_deployment.approved_by == "reviewer-1"
    assert restarted.get_application(application.id).name == application.name
    _, lines = restarted.get_pipeline_logs(run.id)
    assert "stage=build" in lines
    assert "stage=publish" in lines
    assert any(line.startswith("approved by=reviewer-1") for line in lines)


def test_delivery_events_survive_a_restart_so_dora_is_reproducible(database):
    platform = DeliveryPlatform()
    _, run = seed(platform, unique_name())
    platform.record_ci_result(run.id, PipelineStatus.RUNNING.value, None, [])
    deployment = platform.record_ci_result(run.id, PipelineStatus.SUCCEEDED.value, DIGEST, []).deployment
    assert deployment is not None
    platform.approve_deployment(deployment.id, "reviewer-1")
    platform.record_deployment_result(deployment.id, DeploymentStatus.HEALTHY.value, "ok")
    before = platform.delivery_events()

    restarted = DeliveryPlatform()
    after = restarted.delivery_events()

    assert len(after) == len(before) == 2
    assert {event.event_type for event in after} == {DeliveryEventType.COMMIT, DeliveryEventType.DEPLOYMENT}
    assert {event.id for event in after} == {event.id for event in before}


def test_an_idempotency_key_still_replays_after_a_restart(database):
    name = unique_name()
    platform = DeliveryPlatform()
    application, run = seed(platform, name)

    restarted = DeliveryPlatform()
    replayed_application = restarted.create_application(
        name=name,
        repository_url=f"https://github.com/example/{name}",
        pipeline_template="container-ci-cd-v1",
        runtime=Runtime.DOCKER,
        default_environment=Environment.DEV,
        stages=[],
        idempotency_key=f"create-{name}",
    )
    replayed_run = restarted.start_pipeline(
        application.id,
        commit_sha="abc1234",
        branch="main",
        environment=Environment.PROD,
        parameters={},
        correlation_id="correlation-durable",
        idempotency_key=f"start-{name}",
    )

    assert replayed_application.id == application.id
    assert replayed_run.id == run.id
    assert len(restarted.list_pipeline_runs(application.id)) == 1


def test_the_same_key_with_a_different_payload_still_conflicts_after_a_restart(database):
    name = unique_name()
    platform = DeliveryPlatform()
    application, _ = seed(platform, name)

    restarted = DeliveryPlatform()

    with pytest.raises(DeliveryError) as failure:
        restarted.start_pipeline(
            application.id,
            commit_sha="different-commit",
            branch="main",
            environment=Environment.PROD,
            parameters={},
            correlation_id="correlation-durable",
            idempotency_key=f"start-{name}",
        )

    assert failure.value.code == "IDEMPOTENCY_KEY_REUSED"


def test_two_processes_racing_the_same_transition_do_not_both_win(database):
    """Compare-and-set is what stops a duplicated callback from double-applying."""

    first = DeliveryPlatform()
    _, run = seed(first, unique_name())
    # A second process that loaded the same state before either wrote.
    second = DeliveryPlatform()
    assert second.get_pipeline(run.id).version == first.get_pipeline(run.id).version

    first.record_ci_result(run.id, PipelineStatus.RUNNING.value, None, ["from-first"])

    with pytest.raises(DeliveryError) as failure:
        second.record_ci_result(run.id, PipelineStatus.RUNNING.value, None, ["from-second"])

    assert failure.value.code == "CONCURRENT_MODIFICATION"
    assert failure.value.status_code == 409
    assert DeliveryPlatform().get_pipeline(run.id).version == run.version + 1


def test_a_persistence_failure_is_reported_rather_than_silently_kept_in_memory(monkeypatch, database):
    platform = DeliveryPlatform()
    application, _ = seed(platform, unique_name())
    # Point at a port nothing listens on: the database is up but unreachable.
    monkeypatch.setenv("DATABASE_URL", "postgresql://netci:netci-local-only@127.0.0.1:1/netci?connect_timeout=1")
    broken = DeliveryPlatform()
    broken._applications[application.id] = application  # noqa: SLF001 - state that load could not fetch

    with pytest.raises(DeliveryError) as failure:
        broken.start_pipeline(
            application.id,
            commit_sha="abc1234",
            branch="main",
            environment=Environment.DEV,
            parameters={},
            correlation_id="correlation-broken",
            idempotency_key=None,
        )

    assert failure.value.code == "PERSISTENCE_UNAVAILABLE"
    assert failure.value.status_code == 503


def test_security_evidence_decisions_are_written_to_the_audit_trail(database):
    import psycopg

    platform = DeliveryPlatform()
    _, run = seed(platform, unique_name(), environment=Environment.STAGING)
    platform.record_ci_result(run.id, PipelineStatus.RUNNING.value, None, [])
    platform.record_security_evidence(
        run.id,
        {
            "artifactDigest": DIGEST,
            "sbom": {"generatedBy": "syft", "location": "s3://netci-evidence/sbom.json"},
            "vulnerabilityScan": {"scanner": "trivy", "status": "passed", "critical": 0, "high": 0},
            "signature": {"provider": "cosign", "verified": True},
        },
    )

    with psycopg.connect(DATABASE_URL) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT event_type, payload FROM audit_events WHERE pipeline_run_id = %s ORDER BY occurred_at",
                (run.id,),
            )
            rows = cursor.fetchall()

    recorded = {row[0]: row[1] for row in rows}
    assert "artifact.evidence_recorded" in recorded
    assert recorded["artifact.evidence_recorded"]["decision"] == "allow"


def test_security_evidence_and_its_audit_identity_survive_a_restart(database):
    platform = DeliveryPlatform()
    _, run = seed(platform, unique_name(), environment=Environment.STAGING)
    evidence = {
        "artifactDigest": DIGEST,
        "sbom": {"generatedBy": "syft", "location": "s3://netci-evidence/sbom.json"},
        "vulnerabilityScan": {"scanner": "trivy", "status": "passed", "critical": 0, "high": 0},
        "signature": {"provider": "cosign", "verified": True},
    }
    platform.record_security_evidence(run.id, evidence)
    audit_id = next(
        record.id for record in platform.audit_records() if record.event_type == "artifact.evidence_recorded"
    )

    restarted = DeliveryPlatform()

    assert restarted.security_evidence(run.id)["artifactDigest"] == DIGEST
    assert next(
        record.id for record in restarted.audit_records() if record.event_type == "artifact.evidence_recorded"
    ) == audit_id


def test_a_rollback_is_durable_and_keeps_the_superseded_digest(database):
    platform = DeliveryPlatform()
    _, run = seed(platform, unique_name())
    platform.record_ci_result(run.id, PipelineStatus.RUNNING.value, None, [])
    deployment = platform.record_ci_result(run.id, PipelineStatus.SUCCEEDED.value, DIGEST, []).deployment
    assert deployment is not None
    platform.approve_deployment(deployment.id, "reviewer-1")
    platform.record_deployment_result(deployment.id, DeploymentStatus.HEALTHY.value, "ok")
    platform.rollback_deployment(deployment.id, ROLLBACK_DIGEST)

    restarted = DeliveryPlatform()
    recovered = restarted.get_deployment(deployment.id)

    assert recovered.status == DeploymentStatus.ROLLED_BACK
    assert recovered.artifact_digest == ROLLBACK_DIGEST
    assert recovered.previous_artifact_digest == DIGEST
    assert restarted.get_pipeline(run.id).status == PipelineStatus.ROLLED_BACK


# ------------------------------------------------------- deletes that must not lie


def _portal_with_store():
    """A PortalReadModel backed by the real database, as the API composes it."""

    from app.persistence import PostgresPortalStore
    from app.portal import PortalReadModel

    platform = DeliveryPlatform()
    portal = PortalReadModel(platform, store=PostgresPortalStore(DATABASE_URL))
    return portal


def _module_payload(name: str) -> dict:
    return {
        "name": name,
        "displayName": name,
        "repositoryUrl": "https://git.example.com/team/app",
        "pipelineTemplate": "container-ci-cd-v1",
        "runtime": Runtime.DOCKER,
        "module_type": "Backend",
        "description": "delete probe",
    }


@pytest.mark.skipif(not DATABASE_URL, reason="NETCI_TEST_DATABASE_URL is not set")
def test_a_refused_delete_is_reported_instead_of_reappearing_after_a_restart():
    """The bug this covers: `except Exception: pass` around the store delete.

    A module named by a production request cannot be deleted -- the foreign key is ON
    DELETE RESTRICT. Swallowing that returned 204, removed the module from memory, left
    the row in the database, and brought it back on the next restart. A delete the user
    is told worked, that silently undoes itself, is worse than one that refuses.
    """

    from app.persistence import PostgresPortalStore, StillReferenced

    store = PostgresPortalStore(DATABASE_URL)
    module_id = f"probe-{uuid.uuid4().hex[:8]}"

    # Reach past the read model: this test is about the store's contract.
    with store._connect() as connection:  # noqa: SLF001 - the contract under test
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO systems (id, unit, description, owner, status) VALUES (%s,%s,%s,%s,%s)"
                " ON CONFLICT (id) DO NOTHING",
                (module_id, "probe", "delete probe", "tester", "healthy"),
            )
            cursor.execute(
                "INSERT INTO modules (id, system_id, name, module_type, description, runtime)"
                " VALUES (%s,%s,%s,%s,%s,%s)",
                (module_id, module_id, module_id, "Backend", "delete probe", "docker"),
            )
            request_id = uuid.uuid4()
            # production_requests still carries the legacy single-module column alongside
            # the join table; both name the module, and both must be satisfied.
            cursor.execute(
                "INSERT INTO production_requests (id, module_id, requested_by, status)"
                " VALUES (%s,%s,%s,%s)",
                (request_id, module_id, "tester", "waiting_approval"),
            )
            cursor.execute(
                "INSERT INTO production_request_modules (request_id, module_id, version, deployment_order)"
                " VALUES (%s,%s,%s,%s)",
                (request_id, module_id, "v1.0.0", 1),
            )

    with pytest.raises(StillReferenced, match="still referenced"):
        store.delete_module(module_id)

    # And the row is still there, so the refusal was honest.
    with store._connect() as connection:  # noqa: SLF001
        with connection.cursor() as cursor:
            cursor.execute("SELECT count(*) AS total FROM modules WHERE id = %s", (module_id,))
            assert cursor.fetchone()["total"] == 1

    # Clean up in dependency order.
    with store._connect() as connection:  # noqa: SLF001
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM production_request_modules WHERE module_id = %s", (module_id,))
            cursor.execute("DELETE FROM production_requests WHERE id = %s", (request_id,))
            cursor.execute("DELETE FROM modules WHERE id = %s", (module_id,))
            cursor.execute("DELETE FROM systems WHERE id = %s", (module_id,))


@pytest.mark.skipif(not DATABASE_URL, reason="NETCI_TEST_DATABASE_URL is not set")
def test_an_unreferenced_module_really_is_deleted():
    """The other half: a refusal that refuses everything would be just as wrong."""

    from app.persistence import PostgresPortalStore

    store = PostgresPortalStore(DATABASE_URL)
    module_id = f"probe-{uuid.uuid4().hex[:8]}"
    with store._connect() as connection:  # noqa: SLF001
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO systems (id, unit, description, owner, status) VALUES (%s,%s,%s,%s,%s)"
                " ON CONFLICT (id) DO NOTHING",
                (module_id, "probe", "delete probe", "tester", "healthy"),
            )
            cursor.execute(
                "INSERT INTO modules (id, system_id, name, module_type, description, runtime)"
                " VALUES (%s,%s,%s,%s,%s,%s)",
                (module_id, module_id, module_id, "Backend", "delete probe", "docker"),
            )

    store.delete_module(module_id)

    with store._connect() as connection:  # noqa: SLF001
        with connection.cursor() as cursor:
            cursor.execute("SELECT count(*) AS total FROM modules WHERE id = %s", (module_id,))
            assert cursor.fetchone()["total"] == 0
            cursor.execute("DELETE FROM systems WHERE id = %s", (module_id,))
