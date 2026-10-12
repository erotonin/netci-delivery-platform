"""Tests for the optional Claude-generated "suspected cause" suggestion.

This is advisory only: it never changes a run's status and never gates a deployment.
What matters here is that it stays off unless configured, only fires for a FAILED run,
and that secrets planted in the log never reach the prompt sent to the model -- even
when the venv this runs in has no `anthropic` package installed at all.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.adapters import log_summary
from app.adapters.log_summary import ClaudeLogSummarizer
from app.main import app

client = TestClient(app)
MACHINE_HEADERS = {"Authorization": "Bearer netci-local-pipeline-key"}


@pytest.fixture(autouse=True)
def reset_state():
    main.platform.reset()
    main.portal.reset()


def create_application(name: str = "log-summary-app") -> str:
    response = client.post(
        "/applications",
        json={
            "name": name,
            "repositoryUrl": f"https://github.com/example/{name}",
            "pipelineTemplate": "container-ci-cd-v1",
            "runtime": "docker",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def start_run(application_id: str, environment: str = "staging") -> str:
    response = client.post(
        f"/applications/{application_id}/pipeline-runs",
        json={"commitSha": "abc1234", "environment": environment},
    )
    assert response.status_code == 202, response.text
    run_id = response.json()["id"]
    assert client.post(
        f"/pipeline-runs/{run_id}/ci-result", json={"status": "running"}, headers=MACHINE_HEADERS
    ).status_code == 202
    return run_id


def fail_run(run_id: str, log_lines: list[str]) -> None:
    stage = client.post(
        f"/pipeline-runs/{run_id}/stages",
        json={
            "stageId": "build",
            "stageName": "Build",
            "status": "failed",
            "errorMessage": "npm install exited 1",
        },
        headers=MACHINE_HEADERS,
    )
    assert stage.status_code == 202, stage.text
    result = client.post(
        f"/pipeline-runs/{run_id}/ci-result",
        json={"status": "failed", "logLines": log_lines},
        headers=MACHINE_HEADERS,
    )
    assert result.status_code == 202, result.text


def text_block(text: str) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=text)


class FakeMessages:
    def __init__(
        self,
        capture: dict[str, Any],
        response: SimpleNamespace | None = None,
        error: Exception | None = None,
    ) -> None:
        self._capture = capture
        self._response = response
        self._error = error

    def create(self, **kwargs: Any) -> SimpleNamespace:
        self._capture["kwargs"] = kwargs
        if self._error is not None:
            raise self._error
        assert self._response is not None
        return self._response


class FakeClient:
    """Stands in for `anthropic.Anthropic()` -- no network, no `anthropic` import."""

    def __init__(self, response: SimpleNamespace | None = None, error: Exception | None = None) -> None:
        self.capture: dict[str, Any] = {}
        self.beta = SimpleNamespace(messages=FakeMessages(self.capture, response=response, error=error))


def install_summarizer(monkeypatch, fake_client: FakeClient, api_key: str = "sk-ant-not-a-real-key") -> None:
    monkeypatch.setattr(
        main,
        "log_summarizer",
        ClaudeLogSummarizer(api_key=api_key, model="claude-opus-5", client=fake_client),
    )


# ------------------------------------------------------------------ route behaviour


def test_not_configured_returns_501(monkeypatch):
    monkeypatch.setattr(main, "log_summarizer", None)
    application_id = create_application()
    run_id = start_run(application_id)
    fail_run(run_id, ["line one"])

    response = client.post(f"/pipeline-runs/{run_id}/suspected-cause")

    assert response.status_code == 501
    assert response.json()["code"] == "LOG_SUMMARY_NOT_CONFIGURED"


def test_a_succeeded_run_returns_409(monkeypatch):
    fake = FakeClient(
        response=SimpleNamespace(stop_reason="end_turn", content=[text_block("cause")], model="claude-opus-5")
    )
    install_summarizer(monkeypatch, fake)
    application_id = create_application()
    run_id = start_run(application_id)
    succeeded = client.post(
        f"/pipeline-runs/{run_id}/ci-result",
        json={"status": "succeeded", "artifactDigest": "sha256:" + "a" * 64},
        headers=MACHINE_HEADERS,
    )
    assert succeeded.status_code == 202, succeeded.text

    response = client.post(f"/pipeline-runs/{run_id}/suspected-cause")

    assert response.status_code == 409
    assert response.json()["code"] == "RUN_NOT_FAILED"


def test_a_failed_run_returns_a_suggestion_with_the_documented_request_shape(monkeypatch):
    fake = FakeClient(
        response=SimpleNamespace(
            stop_reason="end_turn",
            content=[text_block("The build failed because npm install exited 1.")],
            model="claude-opus-5",
        )
    )
    install_summarizer(monkeypatch, fake)
    application_id = create_application()
    run_id = start_run(application_id)
    fail_run(run_id, ["npm install", "npm ERR! code 1"])

    response = client.post(f"/pipeline-runs/{run_id}/suspected-cause")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["pipelineRunId"] == run_id
    assert body["status"] == "suggested"
    assert "npm install exited 1" in body["text"]
    assert body["model"] == "claude-opus-5"
    assert "suggestion" in body["disclaimer"].lower()

    kwargs = fake.capture["kwargs"]
    assert kwargs["model"] == "claude-opus-5"
    assert kwargs["max_tokens"] == 16000
    assert kwargs["betas"] == ["server-side-fallback-2026-07-01"]
    assert kwargs["fallbacks"] == "default"
    assert kwargs["output_config"] == {"effort": "medium"}
    assert kwargs["system"] == log_summary.SYSTEM_PROMPT
    assert "thinking" not in kwargs
    assert "temperature" not in kwargs
    assert "budget_tokens" not in kwargs

    user_text = kwargs["messages"][0]["content"]
    assert kwargs["messages"][0]["role"] == "user"
    assert "build" in user_text
    assert "npm install exited 1" in user_text
    assert "npm ERR! code 1" in user_text


def test_a_refusal_is_reported_as_declined_with_no_text(monkeypatch):
    fake = FakeClient(response=SimpleNamespace(stop_reason="refusal", content=[], model="claude-opus-5"))
    install_summarizer(monkeypatch, fake)
    application_id = create_application()
    run_id = start_run(application_id)
    fail_run(run_id, ["some log line"])

    response = client.post(f"/pipeline-runs/{run_id}/suspected-cause")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "declined"
    assert body["text"] == ""


def test_planted_secrets_in_the_log_never_reach_the_prompt(monkeypatch):
    fake = FakeClient(
        response=SimpleNamespace(stop_reason="end_turn", content=[text_block("cause")], model="claude-opus-5")
    )
    install_summarizer(monkeypatch, fake)
    application_id = create_application()
    run_id = start_run(application_id)
    fail_run(
        run_id,
        [
            "Authorization: Bearer abc.def.ghi",
            "fetching secrets: password=hunter2-super-secret",
            "-----BEGIN RSA PRIVATE KEY-----",
            "MIIEpAIBAAKCAQEA1234567890abcdefghijklmnop",
            "-----END RSA PRIVATE KEY-----",
            "npm ERR! code 1",
        ],
    )

    response = client.post(f"/pipeline-runs/{run_id}/suspected-cause")

    assert response.status_code == 200, response.text
    user_text = fake.capture["kwargs"]["messages"][0]["content"]
    assert "abc.def.ghi" not in user_text
    assert "Bearer abc.def.ghi" not in user_text
    assert "hunter2-super-secret" not in user_text
    assert "MIIEpAIBAAKCAQEA1234567890abcdefghijklmnop" not in user_text
    assert "PRIVATE KEY-----" not in user_text
    # The rest of the log is still useful to an engineer.
    assert "npm ERR! code 1" in user_text


# -------------------------------------------------------------- error handling


class FakeRateLimitError(Exception):
    pass


class FakeAPIStatusError(Exception):
    pass


class FakeAPIConnectionError(Exception):
    pass


@pytest.fixture(autouse=True)
def fake_anthropic_error_types(monkeypatch):
    # The real `anthropic` package is not installed in this venv. Setting the module's
    # lazily-resolved tuple directly means `_anthropic_error_types_()` never tries to
    # `import anthropic` -- exactly the path notes/adapters/log_summary.py documents.
    monkeypatch.setattr(
        log_summary,
        "_anthropic_error_types",
        (FakeRateLimitError, FakeAPIStatusError, FakeAPIConnectionError),
    )


def test_api_status_error_is_reported_as_502_without_the_key(monkeypatch):
    secret_key = "sk-ant-super-secret-do-not-leak"
    fake = FakeClient(error=FakeAPIStatusError(f"upstream rejected credentials: {secret_key}"))
    install_summarizer(monkeypatch, fake, api_key=secret_key)
    application_id = create_application()
    run_id = start_run(application_id)
    fail_run(run_id, ["boom"])

    response = client.post(f"/pipeline-runs/{run_id}/suspected-cause")

    assert response.status_code == 502
    body = response.json()
    assert body["code"] == "LOG_SUMMARY_FAILED"
    assert secret_key not in response.text


@pytest.mark.parametrize(
    "error",
    [FakeRateLimitError("rate limited"), FakeAPIStatusError("bad status"), FakeAPIConnectionError("no route")],
)
def test_each_documented_anthropic_error_becomes_a_502(monkeypatch, error):
    fake = FakeClient(error=error)
    install_summarizer(monkeypatch, fake)
    application_id = create_application()
    run_id = start_run(application_id)
    fail_run(run_id, ["boom"])

    response = client.post(f"/pipeline-runs/{run_id}/suspected-cause")

    assert response.status_code == 502
    assert response.json()["code"] == "LOG_SUMMARY_FAILED"


# -------------------------------------------------------------- build_log_summarizer


def test_build_log_summarizer_default_is_none(monkeypatch):
    monkeypatch.delenv("NETCI_LOG_SUMMARY", raising=False)
    assert log_summary.build_log_summarizer() is None


def test_build_log_summarizer_none_is_none(monkeypatch):
    monkeypatch.setenv("NETCI_LOG_SUMMARY", "none")
    assert log_summary.build_log_summarizer() is None


def test_build_log_summarizer_unknown_mode_raises(monkeypatch):
    monkeypatch.setenv("NETCI_LOG_SUMMARY", "bogus")
    with pytest.raises(ValueError, match="NETCI_LOG_SUMMARY"):
        log_summary.build_log_summarizer()


def test_build_log_summarizer_claude_without_key_file_raises(monkeypatch):
    monkeypatch.setenv("NETCI_LOG_SUMMARY", "claude")
    monkeypatch.delenv("NETCI_ANTHROPIC_API_KEY_FILE", raising=False)
    with pytest.raises(ValueError, match="NETCI_ANTHROPIC_API_KEY_FILE"):
        log_summary.build_log_summarizer()


def test_build_log_summarizer_claude_with_missing_file_raises(monkeypatch, tmp_path):
    monkeypatch.setenv("NETCI_LOG_SUMMARY", "claude")
    monkeypatch.setenv("NETCI_ANTHROPIC_API_KEY_FILE", str(tmp_path / "does-not-exist"))
    with pytest.raises(ValueError, match="NETCI_ANTHROPIC_API_KEY_FILE"):
        log_summary.build_log_summarizer()


def test_build_log_summarizer_claude_with_empty_file_raises(monkeypatch, tmp_path):
    key_file = tmp_path / "key"
    key_file.write_text("   \n")
    monkeypatch.setenv("NETCI_LOG_SUMMARY", "claude")
    monkeypatch.setenv("NETCI_ANTHROPIC_API_KEY_FILE", str(key_file))
    with pytest.raises(ValueError, match="NETCI_ANTHROPIC_API_KEY_FILE"):
        log_summary.build_log_summarizer()


def test_build_log_summarizer_claude_with_a_valid_key_file_needs_no_anthropic_import(monkeypatch, tmp_path):
    """The `anthropic` package is not installed in this venv -- building must not need it.

    It is only imported lazily, inside `ClaudeLogSummarizer`'s first real call.
    """
    key_file = tmp_path / "key"
    key_file.write_text("sk-ant-test-key\n")
    monkeypatch.setenv("NETCI_LOG_SUMMARY", "claude")
    monkeypatch.setenv("NETCI_ANTHROPIC_API_KEY_FILE", str(key_file))
    monkeypatch.delenv("NETCI_LOG_SUMMARY_MODEL", raising=False)

    summarizer = log_summary.build_log_summarizer()

    assert isinstance(summarizer, log_summary.ClaudeLogSummarizer)
    assert summarizer._model == log_summary.DEFAULT_MODEL == "claude-opus-5"
    assert summarizer._api_key == "sk-ant-test-key"


def test_build_log_summarizer_honours_a_model_override(monkeypatch, tmp_path):
    key_file = tmp_path / "key"
    key_file.write_text("sk-ant-test-key\n")
    monkeypatch.setenv("NETCI_LOG_SUMMARY", "claude")
    monkeypatch.setenv("NETCI_ANTHROPIC_API_KEY_FILE", str(key_file))
    monkeypatch.setenv("NETCI_LOG_SUMMARY_MODEL", "claude-opus-5-custom")

    summarizer = log_summary.build_log_summarizer()

    assert summarizer._model == "claude-opus-5-custom"
