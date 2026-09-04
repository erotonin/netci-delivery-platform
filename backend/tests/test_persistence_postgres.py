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
                " deployments, pipeline_runs, applications, policy_decisions, security_exceptions,"
                " break_glass_requests, resource_quotas, catalog_services, catalog_service_dependencies,"
                " catalog_templates, preview_environments, resource_requests RESTART IDENTITY CASCADE"
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


def test_a_second_transition_after_the_first_is_refused_by_the_state_machine(database):
    """Sequentially, the loser reads the winner's state and is refused on the state rule.

    This is the improvement over caching: the second instance is not working from a
    snapshot it took at start-up, so it sees the transition that already happened.
    """

    first = DeliveryPlatform()
    _, run = seed(first, unique_name())
    second = DeliveryPlatform()

    first.record_ci_result(run.id, PipelineStatus.RUNNING.value, None, ["from-first"])

    with pytest.raises(DeliveryError) as failure:
        second.record_ci_result(run.id, PipelineStatus.RUNNING.value, None, ["from-second"])

    assert failure.value.code == "INVALID_PIPELINE_STATE"
    assert failure.value.status_code == 409
    assert DeliveryPlatform().get_pipeline(run.id).version == run.version + 1


def test_two_replicas_racing_the_same_transition_do_not_both_win(database):
    """Compare-and-set is what stops a duplicated callback from double-applying.

    Both threads read the run at the same version before either writes, so this is the
    race the version column exists for: the second UPDATE blocks on the first one's row
    lock, then matches zero rows because the version moved, and is reported as a conflict.
    """

    import threading

    _, run = seed(DeliveryPlatform(), unique_name())
    replicas = [DeliveryPlatform(), DeliveryPlatform()]
    started = threading.Barrier(len(replicas))
    outcomes: list[object] = []
    lock = threading.Lock()

    def transition(platform: DeliveryPlatform, marker: str) -> None:
        started.wait(timeout=10)
        try:
            platform.record_ci_result(run.id, PipelineStatus.RUNNING.value, None, [marker])
            result: object = "won"
        except DeliveryError as exc:
            result = exc.code
        with lock:
            outcomes.append(result)

    threads = [
        threading.Thread(target=transition, args=(platform, f"from-{index}"))
        for index, platform in enumerate(replicas)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert outcomes.count("won") == 1, outcomes
    # The loser is refused, either on the version check or on the state rule -- both are
    # correct refusals, and which one fires depends on when its read landed.
    assert outcomes.count("won") + sum(
        1 for item in outcomes if item in {"CONCURRENT_MODIFICATION", "INVALID_PIPELINE_STATE"}
    ) == len(replicas), outcomes
    assert DeliveryPlatform().get_pipeline(run.id).version == run.version + 1


def test_a_persistence_failure_is_reported_rather_than_silently_kept_in_memory(monkeypatch, database):
    platform = DeliveryPlatform()
    application, _ = seed(platform, unique_name())
    # Point at a port nothing listens on: the database is up but unreachable.
    monkeypatch.setenv("DATABASE_URL", "postgresql://netci:netci-local-only@127.0.0.1:1/netci?connect_timeout=1")
    broken = DeliveryPlatform()

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
    platform.record_rollback_result(deployment.id, succeeded=True, message="restored")

    restarted = DeliveryPlatform()
    recovered = restarted.get_deployment(deployment.id)

    assert recovered.status == DeploymentStatus.ROLLED_BACK
    assert recovered.artifact_digest == ROLLBACK_DIGEST
    assert recovered.previous_artifact_digest == DIGEST
    assert restarted.get_pipeline(run.id).status == PipelineStatus.ROLLED_BACK


# ------------------------------------------------------- deletes that must not lie


def _portal_with_database():
    """A PortalService backed by the real database, as the API composes it."""

    from app.portal import PortalService
    from app.store import PostgresDatabase

    database = PostgresDatabase(DATABASE_URL)
    return PortalService(DeliveryPlatform(database=database), database=database)


def _execute(*statements):
    """Run raw SQL against the test database, for arranging and cleaning up."""

    import psycopg

    with psycopg.connect(DATABASE_URL) as connection:
        with connection.cursor() as cursor:
            for statement, arguments in statements:
                cursor.execute(statement, arguments)


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

    from app.portal import PortalError

    portal = _portal_with_database()
    module_id = f"probe-{uuid.uuid4().hex[:8]}"
    request_id = uuid.uuid4()
    _execute(
        ("INSERT INTO systems (id, unit, description, owner, status) VALUES (%s,%s,%s,%s,%s)"
         " ON CONFLICT (id) DO NOTHING",
         (module_id, "probe", "delete probe", "tester", "healthy")),
        ("INSERT INTO modules (id, system_id, name, module_type, description, runtime)"
         " VALUES (%s,%s,%s,%s,%s,%s)",
         (module_id, module_id, module_id, "Backend", "delete probe", "docker")),
        # production_requests still carries the legacy single-module column alongside the
        # join table; both name the module, and both must be satisfied.
        ("INSERT INTO production_requests (id, module_id, requested_by, status) VALUES (%s,%s,%s,%s)",
         (request_id, module_id, "tester", "waiting_approval")),
        ("INSERT INTO production_request_modules (request_id, module_id, version, deployment_order)"
         " VALUES (%s,%s,%s,%s)",
         (request_id, module_id, "v1.0.0", 1)),
    )

    with pytest.raises(PortalError) as failure:
        portal.remove_module(module_id)
    assert failure.value.code == "STILL_REFERENCED"
    assert failure.value.status_code == 409

    # And the row is still there, so the refusal was honest.
    with portal.database.transaction() as transaction:
        assert transaction.portal_module(module_id) is not None

    _execute(
        ("DELETE FROM production_request_modules WHERE module_id = %s", (module_id,)),
        ("DELETE FROM production_requests WHERE id = %s", (request_id,)),
        ("DELETE FROM modules WHERE id = %s", (module_id,)),
        ("DELETE FROM systems WHERE id = %s", (module_id,)),
    )


@pytest.mark.skipif(not DATABASE_URL, reason="NETCI_TEST_DATABASE_URL is not set")
def test_an_unreferenced_module_really_is_deleted():
    """The other half: a refusal that refuses everything would be just as wrong."""

    portal = _portal_with_database()
    module_id = f"probe-{uuid.uuid4().hex[:8]}"
    _execute(
        ("INSERT INTO systems (id, unit, description, owner, status) VALUES (%s,%s,%s,%s,%s)"
         " ON CONFLICT (id) DO NOTHING",
         (module_id, "probe", "delete probe", "tester", "healthy")),
        ("INSERT INTO modules (id, system_id, name, module_type, description, runtime)"
         " VALUES (%s,%s,%s,%s,%s,%s)",
         (module_id, module_id, module_id, "Backend", "delete probe", "docker")),
    )

    portal.remove_module(module_id)

    with portal.database.transaction() as transaction:
        assert transaction.portal_module(module_id) is None
    _execute(("DELETE FROM systems WHERE id = %s", (module_id,)))


# ------------------------------------------------- two replicas, one PostgreSQL
#
# These are the assertions the old design could not pass. Each builds two API instances
# the way the composition root does, points both at the same database, and checks that
# they agree without either being restarted.


def _api_instance():
    """One API composition: its own services, sharing the database with every other."""

    from app.portal import PortalService
    from app.store import PostgresDatabase

    database = PostgresDatabase(DATABASE_URL)
    platform = DeliveryPlatform(database=database)
    return platform, PortalService(platform, database=database)


def _onboard(platform, portal, system_id: str, module_id: str, *, idempotency_key=None,
             description="onboarding probe", owner_team=None):
    """Do exactly what POST /systems/{id}/modules does: one transaction for both writes."""

    with platform.database.transaction() as transaction:
        application = platform.create_application(
            name=module_id,
            repository_url=f"https://git.example.com/team/{module_id}",
            pipeline_template="container-ci-cd-v1",
            runtime=Runtime.DOCKER,
            default_environment=Environment.DEV,
            stages=[],
            idempotency_key=idempotency_key,
            owner_team=owner_team,
            session=transaction,
        )
        existing = transaction.portal_module_for_application(application.id)
        if existing is not None:
            return portal.module(existing.id, session=transaction)
        return portal.attach_module(
            system_id=system_id,
            module_id=module_id,
            name=module_id,
            module_type="Backend",
            description=description,
            runtime=Runtime.DOCKER,
            application_id=application.id,
            deployment_environments=[],
            pipeline_config={},
            session=transaction,
        )


@pytest.fixture()
def portal_database(monkeypatch):
    """A clean database, including the Portal hierarchy the delivery fixture leaves alone."""

    import psycopg

    monkeypatch.setenv("DATABASE_URL", DATABASE_URL)

    def wipe():
        with psycopg.connect(DATABASE_URL) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "TRUNCATE production_request_modules, production_requests, release_versions,"
                    " modules, systems, security_evidence, delivery_events, pipeline_logs,"
                    " audit_events, idempotency_records, deployments, pipeline_runs, applications"
                    " RESTART IDENTITY CASCADE"
                )

    wipe()
    yield DATABASE_URL
    wipe()


def test_a_second_replica_sees_a_system_the_first_created_without_restarting(portal_database):
    """The bug this covers: replica B's dictionaries were a snapshot of its own start-up."""

    _, portal_a = _api_instance()
    _, portal_b = _api_instance()
    system_id = f"sys-{uuid.uuid4().hex[:8]}"

    # B is already running and has already answered a request before A writes.
    assert system_id not in {item["id"] for item in portal_b.systems()}

    portal_a.create_system(
        system_id=system_id, unit="Platform", description="cross-replica", owner="tester"
    )

    assert portal_b.system(system_id)["owner"] == "tester"
    assert system_id in {item["id"] for item in portal_b.systems()}


def test_a_second_replica_sees_a_module_the_first_onboarded_without_restarting(portal_database):
    platform_a, portal_a = _api_instance()
    _, portal_b = _api_instance()
    system_id = f"sys-{uuid.uuid4().hex[:8]}"
    module_id = f"mod-{uuid.uuid4().hex[:8]}"
    portal_a.create_system(
        system_id=system_id, unit="Platform", description="cross-replica", owner="tester"
    )

    _onboard(platform_a, portal_a, system_id, module_id)

    assert portal_b.module(module_id)["systemId"] == system_id
    assert [item["id"] for item in portal_b.system(system_id)["modules"]] == [module_id]


def test_an_authorization_query_on_the_second_replica_uses_the_newest_state(portal_database):
    """Authorization is computed from the application list, so a stale one denies access.

    This is the concrete harm of the previous design: replica B refused a team its own
    application because the application did not exist when B started.
    """

    platform_a, portal_a = _api_instance()
    platform_b, _ = _api_instance()
    system_id = f"sys-{uuid.uuid4().hex[:8]}"
    module_id = f"mod-{uuid.uuid4().hex[:8]}"
    portal_a.create_system(
        system_id=system_id, unit="Platform", description="ownership", owner="tester"
    )
    assert platform_b.list_applications() == ()

    _onboard(platform_a, portal_a, system_id, module_id, owner_team="payments")

    visible = {item.name: item.owner_team for item in platform_b.list_applications()}
    assert visible[module_id] == "payments"


def test_a_failure_between_the_application_and_the_module_rolls_both_back(portal_database):
    """The onboarding bug: two transactions left an application no module pointed at."""

    platform, portal = _api_instance()
    system_id = f"sys-{uuid.uuid4().hex[:8]}"
    module_id = f"mod-{uuid.uuid4().hex[:8]}"
    portal.create_system(
        system_id=system_id, unit="Platform", description="fault", owner="tester"
    )

    class InjectedFault(RuntimeError):
        pass

    with pytest.raises(InjectedFault):
        with platform.database.transaction() as transaction:
            platform.create_application(
                name=module_id,
                repository_url=f"https://git.example.com/team/{module_id}",
                pipeline_template="container-ci-cd-v1",
                runtime=Runtime.DOCKER,
                default_environment=Environment.DEV,
                stages=[],
                idempotency_key=f"onboard-{module_id}",
                session=transaction,
            )
            # The process dies here: after the application insert, before the module one.
            raise InjectedFault("worker died between the two writes")

    fresh_platform, fresh_portal = _api_instance()
    assert fresh_platform.list_applications() == (), "an orphan application survived"
    assert fresh_portal.system(system_id)["modules"] == []
    with fresh_platform.database.transaction() as transaction:
        # The idempotency record must be gone too, or the retry would replay an
        # application that no longer exists.
        assert transaction.idempotency("application.create", f"onboard-{module_id}") is None


def test_onboarding_retried_with_the_same_key_returns_the_same_resources(portal_database):
    """A client that times out and retries must get its module, not MODULE_EXISTS."""

    platform, portal = _api_instance()
    system_id = f"sys-{uuid.uuid4().hex[:8]}"
    module_id = f"mod-{uuid.uuid4().hex[:8]}"
    portal.create_system(
        system_id=system_id, unit="Platform", description="retry", owner="tester"
    )
    key = f"onboard-{module_id}"

    first = _onboard(platform, portal, system_id, module_id, idempotency_key=key)
    # The retry arrives at a different replica, as it would behind a load balancer.
    other_platform, other_portal = _api_instance()
    replay = _onboard(other_platform, other_portal, system_id, module_id, idempotency_key=key)

    assert replay["id"] == first["id"]
    assert replay["applicationId"] == first["applicationId"]
    assert len(platform.list_applications()) == 1
    assert len(portal.system(system_id)["modules"]) == 1


def test_onboarding_reusing_a_key_with_a_different_payload_is_a_conflict(portal_database):
    from app.delivery import DeliveryError as Failure

    platform, portal = _api_instance()
    system_id = f"sys-{uuid.uuid4().hex[:8]}"
    module_id = f"mod-{uuid.uuid4().hex[:8]}"
    portal.create_system(
        system_id=system_id, unit="Platform", description="conflict", owner="tester"
    )
    key = f"onboard-{module_id}"
    _onboard(platform, portal, system_id, module_id, idempotency_key=key)

    with pytest.raises(Failure) as failure:
        _onboard(platform, portal, system_id, f"{module_id}-other", idempotency_key=key)

    assert failure.value.code == "IDEMPOTENCY_KEY_REUSED"
    assert failure.value.status_code == 409
    # And the second module was not created by the losing attempt.
    assert len(portal.system(system_id)["modules"]) == 1


def test_two_replicas_onboarding_the_same_module_leave_exactly_one(portal_database):
    """Concurrent onboarding of the same module id must not produce an orphan application."""

    import threading

    platform, portal = _api_instance()
    system_id = f"sys-{uuid.uuid4().hex[:8]}"
    module_id = f"mod-{uuid.uuid4().hex[:8]}"
    portal.create_system(
        system_id=system_id, unit="Platform", description="race", owner="tester"
    )

    started = threading.Barrier(2)
    outcomes: list[str] = []
    lock = threading.Lock()

    def onboard() -> None:
        replica_platform, replica_portal = _api_instance()
        started.wait(timeout=10)
        try:
            _onboard(replica_platform, replica_portal, system_id, module_id)
            result = "created"
        except Exception as exc:  # noqa: BLE001 - the failure mode is the assertion
            result = type(exc).__name__
        with lock:
            outcomes.append(result)

    threads = [threading.Thread(target=onboard) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert outcomes.count("created") == 1, outcomes
    fresh_platform, fresh_portal = _api_instance()
    assert len(fresh_portal.system(system_id)["modules"]) == 1
    # The loser's application was rolled back with its module, so no orphan remains.
    assert len(fresh_platform.list_applications()) == 1


def test_restarting_the_api_changes_no_state(portal_database):
    platform, portal = _api_instance()
    system_id = f"sys-{uuid.uuid4().hex[:8]}"
    module_id = f"mod-{uuid.uuid4().hex[:8]}"
    portal.create_system(
        system_id=system_id, unit="Platform", description="restart", owner="tester"
    )
    _onboard(platform, portal, system_id, module_id)
    before = portal.system(system_id)

    restarted_platform, restarted_portal = _api_instance()

    assert restarted_portal.system(system_id) == before
    assert [item.name for item in restarted_platform.list_applications()] == [module_id]


def test_nothing_is_read_from_the_database_when_the_services_are_composed(portal_database):
    """Start-up must not scan history. It used to materialize every row ever written."""

    from app.store import PostgresDatabase

    opened: list[str] = []

    class CountingDatabase(PostgresDatabase):
        def transaction(self):
            opened.append("transaction")
            return super().transaction()

    from app.portal import PortalService

    database = CountingDatabase(DATABASE_URL)
    platform = DeliveryPlatform(database=database)
    PortalService(platform, database=database)

    assert opened == []


def test_a_racing_duplicate_insert_is_a_conflict_not_a_storage_failure(portal_database):
    """A lost race owes the client 409, not a 503 that points at the database.

    Two transactions can both pass the "does this system exist?" check before either
    commits. The unique index is what actually decides, and its refusal has to be
    translated -- otherwise an operator is sent to look at PostgreSQL for a name clash.
    """

    from app.portal import PortalError
    from app.store import SystemRow

    _, portal = _api_instance()
    system_id = f"sys-{uuid.uuid4().hex[:8]}"
    portal.create_system(
        system_id=system_id, unit="Platform", description="duplicate", owner="tester"
    )

    with pytest.raises(PortalError) as failure:
        # Insert without the pre-check, which is what a concurrent transaction does.
        with portal._session() as transaction:  # noqa: SLF001 - racing the unique index
            transaction.insert_portal_system(
                SystemRow(system_id, "Platform", "duplicate", "tester", "unknown")
            )

    assert failure.value.code == "CONCURRENT_MODIFICATION"
    assert failure.value.status_code == 409
    assert portal.system(system_id)["owner"] == "tester"


def test_the_demo_seed_writes_nothing_unless_it_is_explicitly_enabled(monkeypatch, portal_database):
    """Production builds must start empty; the reference data needs an operator's opt-in."""

    from app.demo_data import demo_data_enabled, seed_demo_data

    monkeypatch.delenv("NETCI_DEMO_DATA", raising=False)
    platform, portal = _api_instance()

    assert demo_data_enabled() is False
    assert seed_demo_data(platform, portal) is False
    assert portal.systems() == []
    assert platform.list_applications() == ()

    monkeypatch.setenv("NETCI_DEMO_DATA", "true")
    assert seed_demo_data(platform, portal) is True
    assert {item["id"] for item in portal.systems()} == {
        "hello-container", "hello-kubernetes", "hello-systemd-go"
    }
    # Seeding again is a no-op rather than a duplicate-key failure.
    assert seed_demo_data(platform, portal) is False
    assert len(platform.list_applications()) == 3


def test_governance_records_persist_in_postgres(monkeypatch, portal_database):
    """Verify durable PostgreSQL persistence for policy decisions, waivers, break-glass, and quotas."""
    from datetime import datetime, timezone, timedelta
    from uuid import uuid4
    from app.store.postgres import PostgresDatabase
    from app.store.records import (
        PolicyDecisionRecord,
        SecurityExceptionRecord,
        BreakGlassRecord,
        ResourceQuotaRecord,
    )

    db = PostgresDatabase(DATABASE_URL)
    now = datetime.now(timezone.utc)

    with db.transaction() as session:
        # 1. Policy decision
        decision_id = uuid4()
        session.record_policy_decision(
            PolicyDecisionRecord(
                id=decision_id,
                scope="production_request",
                target_type="production_request",
                target_id="PR-999",
                allowed=True,
                reason="All checks passed",
                risk_score=42,
                checks={"sbom": "pass", "trivy": "pass"},
                rules_evaluated=["environment_permission", "separation_of_duties"],
                evaluator="builtin",
                evaluated_at=now,
                metadata={"risk_level": "medium"},
            )
        )
        decisions, _, _ = session.policy_decisions_paginated(scope="production_request")
        assert len(decisions) >= 1
        found_dec = next(d for d in decisions if d.id == decision_id)
        assert found_dec.risk_score == 42
        assert found_dec.checks["sbom"] == "pass"
        assert "separation_of_duties" in found_dec.rules_evaluated

        # 2. Security exception
        exc_id = uuid4()
        session.insert_security_exception(
            SecurityExceptionRecord(
                id=exc_id,
                cve="CVE-2026-8888",
                artifact_digest=DIGEST,
                owner="dana",
                reason="upstream fix scheduled",
                approved_by="raj",
                status="active",
                created_at=now,
                expires_at=now + timedelta(days=7),
            )
        )
        active_excs = session.security_exceptions(active_only=True, now=now)
        assert any(e.id == exc_id for e in active_excs)

        # Revoke
        revoked = session.revoke_security_exception(exc_id, revoked_by="raj", revoked_at=now)
        assert revoked is True
        active_after = session.security_exceptions(active_only=True, now=now)
        assert not any(e.id == exc_id for e in active_after)

        # 3. Break glass
        bg_id = uuid4()
        session.insert_break_glass_request(
            BreakGlassRecord(
                id=bg_id,
                target_type="artifact",
                target_id=DIGEST,
                requested_by="dana",
                reason="p0 recovery",
                incident_ticket="INC-111",
                status="pending",
                created_at=now,
            )
        )
        appr_bg = session.approve_break_glass_request(
            request_id=bg_id,
            approved_by="raj",
            approved_at=now,
            expires_at=now + timedelta(hours=1),
        )
        assert appr_bg is not None
        assert appr_bg.status == "active"
        active_bg = session.active_break_glass(target_type="artifact", target_id=DIGEST, now=now)
        assert active_bg is not None
        assert active_bg.id == bg_id

        # 4. Resource quota
        quota_rec = ResourceQuotaRecord(
            id=uuid4(),
            scope="team",
            scope_id="core-banking",
            max_concurrent_pipelines=8,
            max_concurrent_deployments=3,
            max_production_requests_per_day=30,
            created_at=now,
            updated_at=now,
        )
        session.set_resource_quota(quota_rec)
        fetched_quota = session.get_resource_quota("team", "core-banking")
        assert fetched_quota is not None
        assert fetched_quota.max_concurrent_pipelines == 8
        assert fetched_quota.max_concurrent_deployments == 3


def test_phase12_catalog_and_self_service_durability(database: str) -> None:
    """Phase 12: Catalog, service dependencies, templates, preview environments, and resource requests persist in real Postgres."""
    from datetime import datetime, timezone, timedelta
    from uuid import uuid4
    from app.store.postgres import PostgresDatabase
    from app.store.records import (
        CatalogServiceRecord,
        ServiceDependencyRecord,
        CatalogTemplateRecord,
        PreviewEnvironmentRecord,
        ResourceRequestRecord,
    )

    db = PostgresDatabase(database)
    platform = DeliveryPlatform()
    now = datetime.now(timezone.utc)

    # Create an application first for foreign keys
    app = platform.create_application(
        name=f"app-{uuid4().hex[:8]}",
        repository_url="https://github.com/org/payments",
        pipeline_template="container-ci-cd-v1",
        runtime=Runtime.DOCKER,
        default_environment=Environment.DEV,
        owner_team="payments-team",
        stages=[],
        idempotency_key=f"create-app-{uuid4().hex[:8]}",
    )
    app_id = app.id

    with db.transaction() as session:
        # 1. Catalog Services
        svc_a = CatalogServiceRecord(
            id="svc-auth-core",
            name="Auth Core Service",
            description="Identity and access management",
            owning_team="security-team",
            tier="tier-0",
            lifecycle="active",
            repo_url="https://github.com/org/auth-core",
            docs_url="https://docs.org/auth",
            metadata={"slo": "99.99%"},
            created_at=now,
            updated_at=now,
        )
        svc_b = CatalogServiceRecord(
            id="svc-pay-api",
            name="Payments API",
            description="Public payment gateway API",
            owning_team="payments-team",
            tier="tier-1",
            lifecycle="active",
            repo_url="https://github.com/org/pay-api",
            docs_url="https://docs.org/pay",
            metadata={"slo": "99.9%"},
            created_at=now,
            updated_at=now,
        )
        session.insert_catalog_service(svc_a)
        session.insert_catalog_service(svc_b)

        fetched_a = session.catalog_service("svc-auth-core")
        assert fetched_a is not None
        assert fetched_a.owning_team == "security-team"
        assert fetched_a.tier == "tier-0"

        # List services
        services, cursor, has_more = session.list_catalog_services(owning_team="payments-team")
        assert len(services) == 1
        assert services[0].id == "svc-pay-api"

        # 2. Service Dependencies
        dep_id = uuid4()
        dep = ServiceDependencyRecord(
            id=dep_id,
            source_service_id="svc-pay-api",
            target_service_id="svc-auth-core",
            dependency_type="sync",
            description="Token validation RPC",
            created_at=now,
        )
        session.insert_service_dependency(dep)
        deps = session.service_dependencies("svc-pay-api")
        assert len(deps) == 1
        assert deps[0].target_service_id == "svc-auth-core"

        # 3. Catalog Templates
        tmpl = CatalogTemplateRecord(
            id="fastapi-service",
            version="v1.0.0",
            name="FastAPI Microservice",
            description="Production-ready FastAPI scaffold",
            category="backend",
            parameters_schema={"type": "object", "properties": {"python_version": {"type": "string"}}},
            pipeline_definition={"stages": ["lint", "test", "build", "deploy"]},
            is_deprecated=False,
            created_at=now,
            updated_at=now,
        )
        session.insert_catalog_template(tmpl)
        fetched_tmpl = session.catalog_template("fastapi-service", "v1.0.0")
        assert fetched_tmpl is not None
        assert fetched_tmpl.category == "backend"

        # 4. Preview Environments
        prv_id = f"prv-pay-{uuid4().hex[:6]}"
        prv = PreviewEnvironmentRecord(
            id=prv_id,
            application_id=app_id,
            pull_request_id="PR-42",
            commit_sha="a" * 40,
            namespace=f"ns-{prv_id}",
            url=f"https://{prv_id}.preview.netci.internal",
            status="active",
            ttl_seconds=86400,
            expires_at=now + timedelta(hours=24),
            created_by="developer-1",
            created_at=now,
        )
        session.insert_preview_environment(prv)
        fetched_prv = session.preview_environment(prv_id)
        assert fetched_prv is not None
        assert fetched_prv.status == "active"
        assert fetched_prv.url == f"https://{prv_id}.preview.netci.internal"

        # 5. Resource Requests
        req_id = uuid4()
        rreq = ResourceRequestRecord(
            id=req_id,
            application_id=app_id,
            team_id="payments-team",
            environment="preview",
            resource_type="postgres_database",
            spec={"size_gb": 10, "version": "16"},
            status="pending_approval",
            status_reason="",
            provider="unconfigured",
            outputs={},
            requested_by="developer-1",
            created_at=now,
            updated_at=now,
        )
        session.insert_resource_request(rreq)
        fetched_rreq = session.resource_request(req_id)
        assert fetched_rreq is not None
        assert fetched_rreq.resource_type == "postgres_database"

        # Update resource request
        updated_rreq = session.update_resource_request(
            req_id,
            status="provider_not_configured",
            status_reason="Terraform/Crossplane provider is not configured",
            provider="terraform",
        )
        assert updated_rreq is not None
        assert updated_rreq.status == "provider_not_configured"


