"""The Jenkins adapter whose builds go through netci-queue (ADR-082).

Each test names what the queue is for: a trigger retried after a lost answer is the same run, a
queue failing over is waited for and not taken for a refusal, the callback token is never on the
command line of a request, a run is read and cancelled by its id -- never by a Jenkins queue item,
which is one replica's.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from uuid import uuid4

import pytest

from app.adapters import ci_launcher
from app.adapters.ci_launcher import CiLaunchRequest
from app.adapters.jenkins_http import JenkinsHttpConfig
from app.adapters.jenkins_queue import JenkinsQueueAdapter, RunQueueConfig, RunQueueError

JOB = "netci-7a1e3c52-0000-4000-8000-000000000001"


class FakeQueue:
    """netci-queue's intake, in memory: accepts, refuses, or is unavailable as told."""

    def __init__(self) -> None:
        self.runs: dict[str, dict] = {}
        self.by_key: dict[str, str] = {}
        self.requests: list[tuple[str, str, dict | None, str]] = []
        self.unavailable = 0  # answer this many submissions 503 first
        self.refuse_with = 0
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # quiet
                pass

            def _send(self, status, payload):
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}") if length else None
                fake.requests.append(("POST", self.path, body, self.headers.get("Authorization", "")))
                if self.path == "/v1/runs":
                    if fake.unavailable > 0:
                        fake.unavailable -= 1
                        return self._send(503, {"detail": "the run was not accepted; retry with the same idempotency key"})
                    if fake.refuse_with:
                        return self._send(fake.refuse_with, {"detail": "this client may not trigger " + body["job"]})
                    key = body["idempotencyKey"]
                    if key in fake.by_key:
                        return self._send(200, fake.runs[fake.by_key[key]])
                    rid = str(uuid4())
                    fake.by_key[key] = rid
                    params = {k: ("(secret)" if k == "NETCI_CALLBACK_TOKEN" else v) for k, v in body["parameters"].items()}
                    fake.runs[rid] = {"id": rid, "job": body["job"], "state": "accepted", "parameters": params}
                    return self._send(202, fake.runs[rid])
                if self.path.endswith("/cancel"):
                    rid = self.path.split("/")[3]
                    if rid not in fake.runs:
                        return self._send(404, {"detail": "no such run"})
                    return self._send(202, fake.runs[rid])
                self._send(404, {})

            def do_GET(self):  # noqa: N802
                fake.requests.append(("GET", self.path, None, self.headers.get("Authorization", "")))
                rid = self.path.rsplit("/", 1)[-1]
                if self.path.startswith("/v1/runs/") and rid in fake.runs:
                    return self._send(200, fake.runs[rid])
                self._send(404, {"detail": "no such run"})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"


@pytest.fixture
def queue():
    q = FakeQueue()
    yield q
    q.server.shutdown()


def _adapter(queue_url: str, retry: float = 10.0, folder: str = "") -> JenkinsQueueAdapter:
    return JenkinsQueueAdapter(
        JenkinsHttpConfig(base_url="http://vip.example", username="netci", api_token="t", folder=folder),
        RunQueueConfig(url=queue_url, token="queue-client-token", timeout_seconds=2, submit_retry_seconds=retry),
    )


def _request(**overrides) -> CiLaunchRequest:
    fields = dict(
        application_id=uuid4(), application_name="payments-api",
        repository_url="https://git.example/payments/payments-api.git",
        pipeline_template="container-ci-cd-v1", runtime="docker", stages=("checkout", "build"),
        pipeline_run_id=uuid4(), commit_sha="a" * 40, branch="main", environment="dev",
        correlation_id="c-1", parameters={},
    )
    fields.update(overrides)
    return CiLaunchRequest(**fields)


def test_a_build_is_submitted_once_keyed_by_its_pipeline_run(queue, monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    adapter = _adapter(queue.url, folder="netci/apps")
    req = _request()
    run = adapter.trigger_ci_run(JOB, req, "per-run-callback-token")
    assert run.run_id.startswith("queue:") and run.status == "queued"
    method, path, body, auth = queue.requests[0]
    assert (method, path) == ("POST", "/v1/runs")
    assert auth == "Bearer queue-client-token"
    assert body["job"] == f"netci/apps/{JOB}"
    assert body["idempotencyKey"] == str(req.pipeline_run_id)
    assert body["parameters"]["NETCI_CALLBACK_TOKEN"] == "per-run-callback-token"
    assert body["parameters"]["NETCI_PIPELINE_RUN_ID"] == str(req.pipeline_run_id)
    # The same pipeline run triggered again (its answer lost, a retried activity): the same run.
    again = adapter.trigger_ci_run(JOB, req, "a-new-token-minted-on-retry")
    assert again.run_id == run.run_id
    assert len(queue.runs) == 1


def test_a_queue_failing_over_is_waited_for_with_the_same_key(queue, monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    queue.unavailable = 3
    req = _request()
    run = _adapter(queue.url).trigger_ci_run(JOB, req, "tok")
    keys = [body["idempotencyKey"] for method, path, body, _ in queue.requests if path == "/v1/runs"]
    assert keys == [str(req.pipeline_run_id)] * 4
    assert run.run_id.startswith("queue:") and len(queue.runs) == 1


def test_a_refusal_is_not_retried_and_a_queue_down_too_long_fails_the_launch(queue, monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    queue.refuse_with = 403
    with pytest.raises(RunQueueError) as refused:
        _adapter(queue.url).trigger_ci_run(JOB, _request(), "tok")
    assert refused.value.status == 403 and len(queue.requests) == 1
    assert "tok" not in str(refused.value)

    queue.refuse_with, queue.unavailable = 0, 10**6
    with pytest.raises(RunQueueError) as down:
        _adapter(queue.url, retry=0.0).trigger_ci_run(JOB, _request(), "tok")
    assert down.value.status == 503


def test_a_queue_that_does_not_answer_at_all_is_retried_then_fails(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    with pytest.raises(RunQueueError) as err:
        _adapter("http://127.0.0.1:9", retry=0.0).trigger_ci_run(JOB, _request(), "tok")
    assert err.value.status == 0


@pytest.mark.parametrize(
    "view, status",
    [
        ({"state": "accepted"}, "queued"),
        ({"state": "dispatched"}, "queued"),
        ({"state": "started", "buildNumber": 7, "buildUrl": "job/x/7/"}, "running"),
        ({"state": "finished", "result": "SUCCESS"}, "succeeded"),
        ({"state": "finished", "result": "FAILURE"}, "failed"),
        ({"state": "finished", "result": "UNSTABLE"}, "failed"),
        ({"state": "finished", "result": "ABORTED"}, "cancelled"),
        # The build record gone: a result the IDP must not act on, not a failure it invents.
        ({"state": "finished", "result": "UNKNOWN"}, "unknown"),
        ({"state": "cancelled"}, "cancelled"),
        ({"state": "refused"}, "failed"),
    ],
)
def test_a_runs_status_is_read_from_the_queue(queue, view, status):
    rid = str(uuid4())
    queue.runs[rid] = {"id": rid, "job": JOB, **view}
    run = _adapter(queue.url).get_status("queue:" + rid)
    assert run.status == status
    assert run.run_id == "queue:" + rid
    if view.get("buildUrl"):
        assert run.console_url == "http://vip.example/job/x/7/"


def test_logs_are_the_builds_console_through_jenkins_and_empty_before_it_exists(queue):
    adapter = _adapter(queue.url)
    calls = []

    def fake_request(method, path, *, body=None, content_type="application/json", use_crumb=True):
        calls.append((method, path))
        return 200, {}, b"line one\nline two\n"

    adapter._request = fake_request  # type: ignore[method-assign]
    waiting = str(uuid4())
    queue.runs[waiting] = {"id": waiting, "job": "netci/apps/" + JOB, "state": "accepted"}
    assert adapter.get_logs("queue:" + waiting) == []
    started = str(uuid4())
    queue.runs[started] = {"id": started, "job": "netci/apps/" + JOB, "state": "started", "buildNumber": 12}
    assert adapter.get_logs("queue:" + started) == ["line one", "line two"]
    assert calls == [("GET", f"/job/netci/job/apps/job/{JOB}/12/consoleText")]


def test_a_run_is_cancelled_through_the_queue_by_its_id(queue):
    rid = str(uuid4())
    queue.runs[rid] = {"id": rid, "job": JOB, "state": "dispatched"}
    _adapter(queue.url).abort("queue:" + rid)
    assert ("POST", f"/v1/runs/{rid}/cancel") in [(m, p) for m, p, _, _ in queue.requests]
    with pytest.raises(RunQueueError):
        _adapter(queue.url).abort("queue:" + str(uuid4()))


def test_a_run_triggered_before_the_queue_is_still_read_from_jenkins(queue):
    adapter = _adapter(queue.url)

    def fake_request(method, path, *, body=None, content_type="application/json", use_crumb=True):
        assert path == f"/job/{JOB}/5/api/json"
        return 200, {}, json.dumps({"building": False, "result": "SUCCESS", "url": "http://vip.example/job/x/5/"}).encode()

    adapter._request = fake_request  # type: ignore[method-assign]
    assert adapter.get_status(f"{JOB}#5").status == "succeeded"
    assert queue.requests == []


def test_the_queue_needs_its_token_and_one_entry_point(monkeypatch):
    monkeypatch.setenv("NETCI_RUN_QUEUE_URL", "http://queue.example:8080")
    monkeypatch.delenv("NETCI_RUN_QUEUE_TOKEN", raising=False)
    monkeypatch.delenv("NETCI_RUN_QUEUE_TOKEN_FILE", raising=False)
    with pytest.raises(ValueError, match="NETCI_RUN_QUEUE_TOKEN"):
        RunQueueConfig.from_env()
    monkeypatch.setenv("NETCI_RUN_QUEUE_TOKEN", "t")
    assert RunQueueConfig.from_env().url == "http://queue.example:8080"
    # Two controllers: netCI would fall through from one to the other after a failed trigger,
    # which with a durable queue is how a build whose answer was lost is built twice.
    monkeypatch.setenv("NETCI_CI_MODE", "jenkins")
    monkeypatch.setenv("NETCI_JENKINS_CONTROLLERS", "A,B")
    monkeypatch.setenv("NETCI_CALLBACK_URL", "http://netci.example")
    for c in ("A", "B"):
        monkeypatch.setenv(f"JENKINS_{c}_URL", "http://vip.example")
        monkeypatch.setenv(f"JENKINS_{c}_USERNAME", "netci")
        monkeypatch.setenv(f"JENKINS_{c}_API_TOKEN", "t")
    with pytest.raises(ValueError, match="exactly one entry"):
        ci_launcher.build_ci_launcher()
    monkeypatch.setenv("NETCI_JENKINS_CONTROLLERS", "A")
    launcher = ci_launcher.build_ci_launcher()
    assert all(isinstance(a, JenkinsQueueAdapter) for a in launcher.adapters.values())


def test_errors_never_carry_the_callback_token(queue, monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    queue.refuse_with = 422
    with pytest.raises(RunQueueError) as err:
        _adapter(queue.url).trigger_ci_run(JOB, _request(), "secret-callback-token-value")
    assert "secret-callback-token-value" not in str(err.value)
