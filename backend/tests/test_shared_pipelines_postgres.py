"""Shared pipelines on PostgreSQL: the one-active-version rule holds in the database itself,
and a module's pipeline survives a round trip."""

from __future__ import annotations

import threading
import uuid
from dataclasses import replace

import pytest

from app.delivery import DeliveryError, DeliveryPlatform
from app.domain.models import Environment, Runtime
from app import shared_pipelines as sp
from test_persistence_postgres import DATABASE_URL, database  # noqa: F401 - the fixture

pytestmark = pytest.mark.skipif(
    not DATABASE_URL,
    reason="set NETCI_TEST_DATABASE_URL to a migrated PostgreSQL to run durability tests",
)

SCRIPT = "".join(sp.builtin_block(b) for b in sp.CI_BUILTINS)
LINT = SCRIPT.replace("netci-builtin unit-test\n", 'netci-builtin unit-test\n# @stage lint "Lint"\nhadolint Dockerfile\n')


def _platform() -> DeliveryPlatform:
    return DeliveryPlatform()  # the `database` fixture points DATABASE_URL at the test database


def test_a_pipeline_and_a_module_using_it_round_trip(database):
    platform = _platform()
    platform.create_shared_pipeline(name="go-service", description="Go", script=LINT, actor="alice")
    platform.decide_shared_pipeline_version("go-service", 1, approve=True, actor="bob")
    application = platform.create_application(
        name=f"sp-{uuid.uuid4().hex[:6]}", repository_url="https://git.example/sp", pipeline_template="container-ci-cd-v1",
        runtime=Runtime.DOCKER, default_environment=Environment.DEV, stages=[], shared_pipeline="go-service",
        idempotency_key=uuid.uuid4().hex,
    )

    fresh = _platform()
    assert fresh.get_application(application.id).shared_pipeline == "go-service"
    detail = fresh.shared_pipeline_detail("go-service")
    assert detail["activeVersion"] == 1 and detail["usedBy"] == [application.name]
    version = detail["versions"][0]
    assert version["script"] == LINT and version["sha256"] == sp.script_sha256(LINT) and version["decidedBy"] == "bob"
    assert [s["id"] for s in version["stages"]][:2] == ["unit-test", "lint"]


def test_the_database_refuses_a_second_active_version(database):
    platform = _platform()
    platform.create_shared_pipeline(name="go-service", description="", script=SCRIPT, actor="alice")
    platform.decide_shared_pipeline_version("go-service", 1, approve=True, actor="bob")
    platform.propose_shared_pipeline_version("go-service", script=LINT, actor="alice")
    with pytest.raises(Exception):
        with platform.transaction() as tx:
            v2 = next(v for v in tx.shared_pipeline_versions("go-service") if v.version == 2)
            tx.save_shared_pipeline_version(replace(v2, status="active", decided_by="mallory"))
    assert _platform().shared_pipeline_detail("go-service")["activeVersion"] == 1


def test_two_approvers_racing_leave_one_decision(database):
    platform = _platform()
    platform.create_shared_pipeline(name="go-service", description="", script=SCRIPT, actor="alice")
    barrier = threading.Barrier(2)
    outcomes: list[object] = []

    def decide(actor):
        barrier.wait()
        try:
            outcomes.append(_platform().decide_shared_pipeline_version("go-service", 1, approve=True, actor=actor))
        except DeliveryError as exc:
            outcomes.append(exc.code)

    threads = [threading.Thread(target=decide, args=(a,)) for a in ("bob", "carol")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(o if isinstance(o, str) else "ok" for o in outcomes) == ["PIPELINE_VERSION_DECIDED", "ok"]
    assert _platform().shared_pipeline_detail("go-service")["activeVersion"] == 1


def test_a_refused_script_leaves_no_pipeline_row(database):
    with pytest.raises(DeliveryError):
        _platform().create_shared_pipeline(name="go-service", description="", script="echo hi\n", actor="alice")
    assert _platform().shared_pipelines() == []
