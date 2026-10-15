"""A branch's push and the merge request opened from it build their commit once (ADR-095).

On the HA lab both events of one commit started a build: two digests for one source, both
pushed under the commit's tag. GitLab's own answer is workflow rules that drop the branch
pipeline when a merge request exists; here the second event joins the first one's build.
"""

from __future__ import annotations

import os
import threading
import uuid

import pytest

from app.adapters.ci_launcher import LaunchedCi
from app.delivery import DeliveryPlatform
from app.domain.models import Environment, PipelineStatus, Runtime
from app.store.memory import InMemoryDatabase


class Launcher:
    mode = "recording"

    def __init__(self):
        self.launched: list[uuid.UUID] = []
        self.aborted: list[str] = []
        self._lock = threading.Lock()

    def launch(self, request):
        with self._lock:
            self.launched.append(request.pipeline_run_id)
            return LaunchedCi(controller_id="jenkins-a", external_run_id=f"job#{len(self.launched)}")

    def abort(self, jenkins_run_id):
        self.aborted.append(jenkins_run_id)

    def get_status(self, jenkins_run_id):
        return None


def _app(platform, name="one-build"):
    return platform.create_application(
        name=f"{name}-{uuid.uuid4().hex[:6]}", repository_url="https://git.example/app",
        pipeline_template="container-ci-cd-v1", runtime=Runtime.DOCKER, default_environment=Environment.DEV,
        stages=[], idempotency_key=uuid.uuid4().hex,
    )


def _event(platform, application, sha, *, event, branch="feature/x", number=None, fork=False, deploy=False,
           publish=True, cancel=None, key=None):
    trigger = {"event": event, "branch": branch, "fromFork": fork, "deliveryId": key or uuid.uuid4().hex}
    if number is not None:
        trigger["pullRequest"] = number
    return platform.start_pipeline(
        application.id, commit_sha=sha, branch=branch, environment=Environment.DEV, parameters={},
        correlation_id="c", idempotency_key=f"scm:{trigger['deliveryId']}", deploy_after_build=deploy,
        publish_artifact=publish, trigger=trigger,
        cancel_in_progress=(event == "pull_request") if cancel is None else cancel,
    )


def _push(platform, application, sha, **kw):
    return _event(platform, application, sha, event="push", **kw)


def _mr(platform, application, sha, number=2, **kw):
    return _event(platform, application, sha, event="pull_request", number=number, **kw)


def _audits(platform, run_id, action):
    with platform.transaction() as tx:
        return [a for a in tx.audit_records() if a.event_type == action and a.pipeline_run_id == run_id]


def test_a_merge_request_joins_the_build_its_branch_push_started():
    launcher = Launcher()
    platform = DeliveryPlatform(database=InMemoryDatabase(), ci_launcher=launcher)
    application = _app(platform)
    pushed = _push(platform, application, "a" * 40)
    joined = _mr(platform, application, "a" * 40)

    assert joined.id == pushed.id
    assert launcher.launched == [pushed.id]
    assert [r.id for r in platform.list_pipeline_runs(application.id)] == [pushed.id]
    assert "pull_request event (merge request !2) for this commit joined this build" in "\n".join(
        platform.get_pipeline_logs(pushed.id)[1])
    assert _audits(platform, pushed.id, "pipeline.joined")[0].payload["pullRequest"] == 2


def test_the_push_that_comes_second_joins_the_merge_requests_build():
    launcher = Launcher()
    platform = DeliveryPlatform(database=InMemoryDatabase(), ci_launcher=launcher)
    application = _app(platform)
    first = _mr(platform, application, "a" * 40)
    assert _push(platform, application, "a" * 40).id == first.id
    assert launcher.launched == [first.id]


def test_a_finished_build_is_joined_but_a_failed_one_is_built_again():
    launcher = Launcher()
    platform = DeliveryPlatform(database=InMemoryDatabase(), ci_launcher=launcher)
    application = _app(platform)
    ok = _push(platform, application, "a" * 40)
    platform.record_ci_result(ok.id, PipelineStatus.RUNNING.value, None, [])
    platform.record_ci_result(ok.id, PipelineStatus.SUCCEEDED.value, "sha256:" + "1" * 64, [])
    assert _mr(platform, application, "a" * 40).id == ok.id

    bad = _push(platform, application, "b" * 40)
    platform.record_ci_result(bad.id, PipelineStatus.RUNNING.value, None, [])
    platform.record_ci_result(bad.id, PipelineStatus.FAILED.value, None, ["boom"])
    again = _mr(platform, application, "b" * 40)
    assert again.id != bad.id and launcher.launched[-1] == again.id


def test_what_differs_is_built_separately():
    platform = DeliveryPlatform(database=InMemoryDatabase(), ci_launcher=Launcher())
    application = _app(platform)
    build_only = _push(platform, application, "a" * 40)
    # A fork's merge request is in its own group, and verifies without publishing.
    fork = _mr(platform, application, "a" * 40, fork=True, publish=False)
    assert fork.id != build_only.id and fork.concurrency_group.endswith(":pr/2")
    # A push that deploys builds what it deploys; it never takes a build-only run's place.
    deploying = _push(platform, application, "a" * 40, branch="main", deploy=True)
    main_build = _push(platform, application, "c" * 40, branch="main")
    assert deploying.id != build_only.id and main_build.id != deploying.id
    # Another branch is another group.
    other = _push(platform, application, "a" * 40, branch="feature/y")
    assert other.id != build_only.id


def test_a_retried_delivery_of_a_joined_event_answers_with_the_same_build():
    platform = DeliveryPlatform(database=InMemoryDatabase(), ci_launcher=Launcher())
    application = _app(platform)
    pushed = _push(platform, application, "a" * 40)
    assert _mr(platform, application, "a" * 40, key="delivery-7").id == pushed.id
    assert _mr(platform, application, "a" * 40, key="delivery-7").id == pushed.id
    assert len(_audits(platform, pushed.id, "pipeline.joined")) == 1


def test_a_merge_requests_new_commit_stops_the_branchs_older_build_whichever_event_comes_first():
    launcher = Launcher()
    platform = DeliveryPlatform(database=InMemoryDatabase(), ci_launcher=launcher)
    application = _app(platform)
    old = _push(platform, application, "a" * 40)
    assert _mr(platform, application, "a" * 40).id == old.id
    platform.record_ci_result(old.id, PipelineStatus.RUNNING.value, None, [])

    # The new commit's push arrives first: a push does not cancel a running build...
    new = _push(platform, application, "b" * 40)
    assert platform.get_pipeline(old.id).status == PipelineStatus.RUNNING
    # ...its merge request's update does, and joins the new commit's build.
    assert _mr(platform, application, "b" * 40).id == new.id
    stopped = platform.get_pipeline(old.id)
    assert stopped.status == PipelineStatus.CANCELLED and stopped.superseded_by == new.id
    assert launcher.aborted == [stopped.jenkins_run_id]


DATABASE_URL = os.getenv("NETCI_TEST_DATABASE_URL", "").strip()


@pytest.mark.skipif(not DATABASE_URL, reason="needs NETCI_TEST_DATABASE_URL")
def test_a_push_and_its_merge_request_arriving_together_build_once(monkeypatch):
    from test_persistence_postgres import truncate

    monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
    truncate()
    try:
        platform = DeliveryPlatform()
        application = _app(platform, "pg-join")
        for round_ in range(5):
            sha = f"{round_:040x}"
            barrier = threading.Barrier(2)
            results: list = []
            errors: list[BaseException] = []

            def send(kind):
                try:
                    replica = DeliveryPlatform()  # two API replicas, one database
                    barrier.wait()
                    results.append((_push if kind == "push" else _mr)(replica, application, sha).id)
                except BaseException as exc:  # noqa: BLE001 - asserted below
                    errors.append(exc)

            threads = [threading.Thread(target=send, args=(k,)) for k in ("push", "mr")]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=30)
            assert errors == []
            assert len(set(results)) == 1, f"round {round_}: one commit built {len(set(results))} times"
            assert len([r for r in platform.list_pipeline_runs(application.id) if r.commit_sha == sha]) == 1
    finally:
        truncate()
