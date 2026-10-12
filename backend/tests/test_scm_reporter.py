"""Tests for the SCM reporter adapter and its wiring into the notification outbox.

`delivery.py` enqueues NotificationRecord rows (event_type "scm.commit_status" /
"scm.pr_comment", recipient "scm"); these tests cover what happens next: ScmReporter
turning them into real GitHub/GitLab API calls, and RoutingNotificationDispatcher
sending them there instead of through the generic webhook dispatcher.
"""

from __future__ import annotations

import json
from uuid import uuid4

import httpx
import pytest

from app.adapters.scm_reporter import ScmReporter, ScmReportingError
from app.domain.models import NotificationRecord, NotificationStatus
from app.notifications import NotificationOutboxWorker, RoutingNotificationDispatcher
from app.store.memory import InMemoryDatabase


def _record(event_type: str, payload: dict, **overrides) -> NotificationRecord:
    defaults = dict(
        id=uuid4(),
        event_type=event_type,
        aggregate_type="pipeline_run",
        aggregate_id="run-1",
        payload=payload,
        recipient="scm",
        status=NotificationStatus.PENDING,
        attempt=0,
        max_attempts=5,
    )
    defaults.update(overrides)
    return NotificationRecord(**defaults)


def _no_request_transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected request to {request.url}")

    return httpx.MockTransport(handler)


def _recording_transport(status_code: int = 200, body: str = "{}"):
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(status_code, text=body)

    return httpx.MockTransport(handler), captured


GITHUB_STATUS_PAYLOAD = {
    "provider": "github",
    "repository": "octo-org/octo-repo",
    "credentialReference": None,
    "commitSha": "a" * 40,
    "state": "success",
    "context": "netci/pipeline",
    "description": "build succeeded",
    "targetUrl": "https://netci.example/runs/1",
}

GITLAB_STATUS_PAYLOAD = {
    "provider": "gitlab",
    "repository": "group/sub-group/project",
    "credentialReference": None,
    "commitSha": "b" * 40,
    "state": "success",
    "context": "netci/pipeline",
    "description": "build succeeded",
    "targetUrl": None,
}


# -- GitHub commit status -----------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "netci_state,github_state",
    [
        ("pending", "pending"),
        ("running", "pending"),
        ("success", "success"),
        ("failure", "failure"),
        ("cancelled", "error"),
    ],
)
async def test_github_commit_status_state_mapping(monkeypatch, netci_state, github_state):
    monkeypatch.setenv("NETCI_GITHUB_TOKEN", "gh-secret-token")
    transport, captured = _recording_transport(201)
    reporter = ScmReporter(transport=transport)
    payload = dict(GITHUB_STATUS_PAYLOAD, state=netci_state)

    await reporter.dispatch(_record("scm.commit_status", payload))

    assert len(captured) == 1
    request = captured[0]
    assert request.method == "POST"
    assert str(request.url) == f"https://api.github.com/repos/octo-org/octo-repo/statuses/{'a' * 40}"
    assert request.headers["authorization"] == "Bearer gh-secret-token"
    assert request.headers["accept"] == "application/vnd.github+json"
    assert request.headers["x-github-api-version"] == "2022-11-28"
    body = json.loads(request.content)
    assert body["state"] == github_state
    assert body["context"] == "netci/pipeline"
    assert body["description"] == "build succeeded"
    assert body["target_url"] == "https://netci.example/runs/1"


@pytest.mark.asyncio
async def test_github_commit_status_omits_target_url_when_null(monkeypatch):
    monkeypatch.setenv("NETCI_GITHUB_TOKEN", "gh-secret-token")
    transport, captured = _recording_transport(201)
    reporter = ScmReporter(transport=transport)
    payload = dict(GITHUB_STATUS_PAYLOAD, targetUrl=None)

    await reporter.dispatch(_record("scm.commit_status", payload))

    body = json.loads(captured[0].content)
    assert "target_url" not in body


@pytest.mark.asyncio
async def test_github_commit_status_truncates_description(monkeypatch):
    monkeypatch.setenv("NETCI_GITHUB_TOKEN", "gh-secret-token")
    transport, captured = _recording_transport(201)
    reporter = ScmReporter(transport=transport)
    payload = dict(GITHUB_STATUS_PAYLOAD, description="x" * 500)

    await reporter.dispatch(_record("scm.commit_status", payload))

    body = json.loads(captured[0].content)
    assert len(body["description"]) == 140


# -- GitHub PR comment ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_github_pr_comment(monkeypatch):
    monkeypatch.setenv("NETCI_GITHUB_TOKEN", "gh-secret-token")
    transport, captured = _recording_transport(201)
    reporter = ScmReporter(transport=transport)
    payload = {
        "provider": "github",
        "repository": "octo-org/octo-repo",
        "credentialReference": None,
        "pullRequest": 42,
        "body": "Build **succeeded**.",
    }

    await reporter.dispatch(_record("scm.pr_comment", payload))

    assert len(captured) == 1
    request = captured[0]
    assert str(request.url) == "https://api.github.com/repos/octo-org/octo-repo/issues/42/comments"
    assert request.headers["authorization"] == "Bearer gh-secret-token"
    assert json.loads(request.content) == {"body": "Build **succeeded**."}


# -- GitLab commit status -------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "netci_state,gitlab_state",
    [
        ("pending", "pending"),
        ("running", "running"),
        ("success", "success"),
        ("failure", "failed"),
        ("cancelled", "canceled"),
    ],
)
async def test_gitlab_commit_status_state_mapping(monkeypatch, netci_state, gitlab_state):
    monkeypatch.setenv("NETCI_GITLAB_TOKEN", "gl-secret-token")
    transport, captured = _recording_transport(201)
    reporter = ScmReporter(transport=transport)
    payload = dict(GITLAB_STATUS_PAYLOAD, state=netci_state)

    await reporter.dispatch(_record("scm.commit_status", payload))

    assert len(captured) == 1
    request = captured[0]
    # "group/sub-group/project" must be fully percent-encoded, including the slashes,
    # or GitLab resolves it as a path lookup instead of a project id.
    assert str(request.url) == (
        "https://gitlab.com/api/v4/projects/group%2Fsub-group%2Fproject/statuses/" + "b" * 40
    )
    assert request.headers["private-token"] == "gl-secret-token"
    body = json.loads(request.content)
    assert body["state"] == gitlab_state
    assert body["name"] == "netci/pipeline"
    assert body["description"] == "build succeeded"
    assert "target_url" not in body  # payload's targetUrl is None


# -- GitLab MR note ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_gitlab_mr_comment(monkeypatch):
    monkeypatch.setenv("NETCI_GITLAB_TOKEN", "gl-secret-token")
    transport, captured = _recording_transport(201)
    reporter = ScmReporter(transport=transport)
    payload = {
        "provider": "gitlab",
        "repository": "group/project",
        "credentialReference": None,
        "pullRequest": 7,
        "body": "Build succeeded.",
    }

    await reporter.dispatch(_record("scm.pr_comment", payload))

    request = captured[0]
    assert str(request.url) == "https://gitlab.com/api/v4/projects/group%2Fproject/merge_requests/7/notes"
    assert request.headers["private-token"] == "gl-secret-token"
    assert json.loads(request.content) == {"body": "Build succeeded."}


# -- refusals ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_github_token_raises_and_makes_no_request(monkeypatch):
    monkeypatch.delenv("NETCI_GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("NETCI_GITHUB_TOKEN_FILE", raising=False)
    reporter = ScmReporter(transport=_no_request_transport())

    with pytest.raises(ScmReportingError, match="NETCI_GITHUB_TOKEN_FILE is not configured"):
        await reporter.dispatch(_record("scm.commit_status", dict(GITHUB_STATUS_PAYLOAD)))


@pytest.mark.asyncio
async def test_missing_gitlab_token_raises_and_makes_no_request(monkeypatch):
    monkeypatch.delenv("NETCI_GITLAB_TOKEN", raising=False)
    monkeypatch.delenv("NETCI_GITLAB_TOKEN_FILE", raising=False)
    reporter = ScmReporter(transport=_no_request_transport())

    with pytest.raises(ScmReportingError, match="NETCI_GITLAB_TOKEN_FILE is not configured"):
        await reporter.dispatch(_record("scm.commit_status", dict(GITLAB_STATUS_PAYLOAD)))


def test_token_file_wins_over_plain_variable(monkeypatch, tmp_path):
    token_file = tmp_path / "github-token"
    token_file.write_text("file-token-value\n")
    monkeypatch.setenv("NETCI_GITHUB_TOKEN", "plain-token-value")
    monkeypatch.setenv("NETCI_GITHUB_TOKEN_FILE", str(token_file))

    reporter = ScmReporter()

    assert reporter._github_token == "file-token-value"


@pytest.mark.asyncio
async def test_unknown_provider_raises(monkeypatch):
    monkeypatch.setenv("NETCI_GITHUB_TOKEN", "gh-secret-token")
    monkeypatch.setenv("NETCI_GITLAB_TOKEN", "gl-secret-token")
    reporter = ScmReporter(transport=_no_request_transport())
    payload = dict(GITHUB_STATUS_PAYLOAD, provider="bitbucket")

    with pytest.raises(ScmReportingError, match="unknown SCM provider"):
        await reporter.dispatch(_record("scm.commit_status", payload))


@pytest.mark.asyncio
async def test_unknown_event_type_raises(monkeypatch):
    reporter = ScmReporter(transport=_no_request_transport())

    with pytest.raises(ScmReportingError, match="unknown SCM notification event type"):
        await reporter.dispatch(_record("scm.something_else", dict(GITHUB_STATUS_PAYLOAD)))


@pytest.mark.asyncio
async def test_invalid_repository_raises(monkeypatch):
    monkeypatch.setenv("NETCI_GITHUB_TOKEN", "gh-secret-token")
    reporter = ScmReporter(transport=_no_request_transport())
    payload = dict(GITHUB_STATUS_PAYLOAD, repository="not a repo")

    with pytest.raises(ScmReportingError, match="invalid SCM repository identifier"):
        await reporter.dispatch(_record("scm.commit_status", payload))


@pytest.mark.asyncio
async def test_invalid_commit_sha_raises(monkeypatch):
    monkeypatch.setenv("NETCI_GITHUB_TOKEN", "gh-secret-token")
    reporter = ScmReporter(transport=_no_request_transport())
    payload = dict(GITHUB_STATUS_PAYLOAD, commitSha="not-hex!!")

    with pytest.raises(ScmReportingError, match="invalid commit sha"):
        await reporter.dispatch(_record("scm.commit_status", payload))


@pytest.mark.asyncio
async def test_invalid_pull_request_number_raises(monkeypatch):
    monkeypatch.setenv("NETCI_GITHUB_TOKEN", "gh-secret-token")
    reporter = ScmReporter(transport=_no_request_transport())
    payload = {
        "provider": "github",
        "repository": "octo-org/octo-repo",
        "credentialReference": None,
        "pullRequest": -1,
        "body": "hi",
    }

    with pytest.raises(ScmReportingError, match="invalid pull request number"):
        await reporter.dispatch(_record("scm.pr_comment", payload))


@pytest.mark.asyncio
async def test_non_2xx_response_raises_without_leaking_token(monkeypatch):
    monkeypatch.setenv("NETCI_GITHUB_TOKEN", "super-secret-token-value")
    transport = httpx.MockTransport(lambda request: httpx.Response(500, text="internal server error, try again"))
    reporter = ScmReporter(transport=transport)

    with pytest.raises(ScmReportingError) as excinfo:
        await reporter.dispatch(_record("scm.commit_status", dict(GITHUB_STATUS_PAYLOAD)))

    message = str(excinfo.value)
    assert "500" in message
    assert "internal server error" in message
    assert "super-secret-token-value" not in message


# -- RoutingNotificationDispatcher ---------------------------------------------------


class _RecordingDispatcher:
    def __init__(self):
        self.dispatched: list[NotificationRecord] = []

    async def dispatch(self, notification: NotificationRecord) -> None:
        self.dispatched.append(notification)


@pytest.mark.asyncio
async def test_routing_dispatcher_sends_scm_events_to_scm_dispatcher():
    scm = _RecordingDispatcher()
    default = _RecordingDispatcher()
    router = RoutingNotificationDispatcher(scm=scm, default=default)
    scm_notification = _record("scm.commit_status", dict(GITHUB_STATUS_PAYLOAD))
    other_notification = _record("pipeline.completed", {"result": "succeeded"})

    await router.dispatch(scm_notification)
    await router.dispatch(other_notification)

    assert scm.dispatched == [scm_notification]
    assert default.dispatched == [other_notification]


# -- outbox round trip ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_outbox_delivers_scm_commit_status_through_router(monkeypatch):
    monkeypatch.setenv("NETCI_GITHUB_TOKEN", "gh-secret-token")
    transport, captured = _recording_transport(201)
    reporter = ScmReporter(transport=transport)
    default = _RecordingDispatcher()
    router = RoutingNotificationDispatcher(scm=reporter, default=default)

    database = InMemoryDatabase()
    notification = _record("scm.commit_status", dict(GITHUB_STATUS_PAYLOAD))
    with database.transaction() as session:
        session.record_notification(notification)

    worker = NotificationOutboxWorker(database, dispatcher=router)
    processed = await worker.run_once()

    assert processed == 1
    assert len(captured) == 1
    with database.transaction() as session:
        delivered = session.notification(notification.id)
        assert delivered is not None
        assert delivered.status == NotificationStatus.DELIVERED
        assert delivered.delivered_at is not None


@pytest.mark.asyncio
async def test_outbox_retries_scm_commit_status_on_failure(monkeypatch):
    monkeypatch.setenv("NETCI_GITHUB_TOKEN", "gh-secret-token")
    transport = httpx.MockTransport(lambda request: httpx.Response(500, text="server error"))
    reporter = ScmReporter(transport=transport)
    default = _RecordingDispatcher()
    router = RoutingNotificationDispatcher(scm=reporter, default=default)

    database = InMemoryDatabase()
    notification = _record("scm.commit_status", dict(GITHUB_STATUS_PAYLOAD))
    with database.transaction() as session:
        session.record_notification(notification)

    worker = NotificationOutboxWorker(database, dispatcher=router)
    processed = await worker.run_once()

    assert processed == 1
    with database.transaction() as session:
        not_delivered = session.notification(notification.id)
        assert not_delivered is not None
        assert not_delivered.status != NotificationStatus.DELIVERED
        assert not_delivered.attempt == notification.attempt + 1
        assert "super-secret" not in (not_delivered.last_error or "")
