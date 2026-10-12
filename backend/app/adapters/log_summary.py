"""Optional "suspected cause" summary for a FAILED pipeline run, read by Claude.

This is a suggestion shown to a person: it never changes a run's status, never gates
a deployment, and is off unless configured (CLAUDE.md non-negotiable #1 -- no fake
success; a wrong guess here is a wrong guess in a text box, not a wrong gate decision).

`NETCI_LOG_SUMMARY` selects the backend:

    none    (default) the feature is off; the route returns 501.
    claude  call the Anthropic API. Requires `NETCI_ANTHROPIC_API_KEY_FILE` to name a
            file holding the key -- fails closed at import time (non-negotiable #2)
            if that file is unset, missing or empty, the same way every other secret
            this platform depends on does (see `adapters/scm_reporter._read_secret`).

The `anthropic` package is imported lazily, inside `ClaudeLogSummarizer`'s first real
call, never at module import: this module (and `build_log_summarizer()`) must import
and be unit-tested on a venv that does not have `anthropic` installed at all, which is
the state of the venv this was built against.
"""

from __future__ import annotations

import os
import re
from typing import Any

from ..logging import redact_sensitive_data

DEFAULT_MODEL = "claude-opus-5"

#: Kept verbatim -- this is the contract with the model, not prose to be improved.
SYSTEM_PROMPT = (
    "You read a failed CI/CD build of a netCI pipeline. Say in at most five short lines "
    "the most likely cause and the first thing to check. Quote the log line that supports "
    "it. If the log does not show the cause, say so. This is a suggestion for an engineer; "
    "do not claim certainty."
)

MAX_LOG_LINES = 400
MAX_LINE_CHARS = 500

# `redact_sensitive_data` (backend/app/logging.py) already scrubs "Bearer <token>". These
# catch the shapes it was never asked to: an Authorization header without the Bearer
# scheme, token=/password= query-string style params, and a PEM private key block --
# all things that show up in raw CI console output.
_PRIVATE_KEY_BLOCK_RE = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
    re.DOTALL,
)
_AUTH_HEADER_RE = re.compile(r"(?i)\bauthorization\s*:\s*\S+")
_TOKEN_PARAM_RE = re.compile(r"(?i)\btoken=[^\s&\"'<>]+")
_PASSWORD_PARAM_RE = re.compile(r"(?i)\bpassword=[^\s&\"'<>]+")


class LogSummaryError(RuntimeError):
    """The summary could not be produced."""


class LogSummaryUnavailable(LogSummaryError):
    """The feature is not configured (`NETCI_LOG_SUMMARY` is unset or `none`)."""


#: Resolved on first use, not at import -- see the module docstring. A test that wants
#: to exercise the RateLimitError/APIStatusError/APIConnectionError handling without the
#: real package installed sets this tuple directly (to fake exception classes) so
#: `_anthropic_error_types()` never has to import `anthropic` at all.
_anthropic_error_types: tuple[type[BaseException], ...] | None = None


def _anthropic_error_types_() -> tuple[type[BaseException], ...]:
    global _anthropic_error_types
    if _anthropic_error_types is None:
        import anthropic  # lazy: see module docstring

        _anthropic_error_types = (
            anthropic.RateLimitError,
            anthropic.APIStatusError,
            anthropic.APIConnectionError,
        )
    return _anthropic_error_types


def _redact_lines(lines: list[str]) -> list[str]:
    """Strip secrets before anything derived from the log leaves this process."""

    # The private key block spans several log lines; catch it across the joined text
    # before splitting back out, then run word/pattern redaction line by line.
    joined = _PRIVATE_KEY_BLOCK_RE.sub("[REDACTED PRIVATE KEY]", "\n".join(lines))
    redacted: list[str] = []
    for line in joined.split("\n"):
        line = redact_sensitive_data(line)
        line = _AUTH_HEADER_RE.sub("Authorization: [REDACTED]", line)
        line = _TOKEN_PARAM_RE.sub("token=[REDACTED]", line)
        line = _PASSWORD_PARAM_RE.sub("password=[REDACTED]", line)
        redacted.append(line)
    return redacted


def _format_stages(stages: list[dict]) -> str:
    failed = [s for s in stages if str(s.get("status", "")).lower() == "failed"] or list(stages)
    lines = [
        "stage {stage_id} ({stage_name}): {status} -- {error}".format(
            stage_id=s.get("stage_id"),
            stage_name=s.get("stage_name"),
            status=s.get("status"),
            error=s.get("error_message") or "(no error message recorded)",
        )
        for s in failed
    ]
    return "\n".join(lines) if lines else "(no stages recorded for this run)"


def _build_user_text(stages: list[dict], log_lines: list[str]) -> str:
    recent = list(log_lines)[-MAX_LOG_LINES:]
    truncated = [line[:MAX_LINE_CHARS] for line in recent]
    redacted = _redact_lines(truncated)
    return (
        f"Failed stages:\n{_format_stages(stages)}\n\n"
        f"Last {len(redacted)} log lines:\n" + "\n".join(redacted)
    )


class ClaudeLogSummarizer:
    """Calls the Anthropic API to read a failed run's log and suggest a cause.

    `client` is injectable so tests never touch the network or need the `anthropic`
    package installed; production leaves it `None` and it is built lazily, from
    `api_key`/`model`, on the first call `summarize()` actually makes.
    """

    def __init__(self, *, api_key: str, model: str, client: Any | None = None) -> None:
        self._api_key = api_key
        self._model = model
        self._client = client

    def _get_client(self) -> Any:
        if self._client is None:
            import anthropic  # lazy: see module docstring

            self._client = anthropic.Anthropic(api_key=self._api_key, max_retries=2, timeout=60.0)
        return self._client

    def summarize(self, *, stages: list[dict], log_lines: list[str]) -> dict:
        user_text = _build_user_text(stages, log_lines)
        client = self._get_client()
        rate_limit_error, api_status_error, api_connection_error = _anthropic_error_types_()
        try:
            response = client.beta.messages.create(
                model=self._model,
                max_tokens=16000,
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
                output_config={"effort": "medium"},
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_text}],
            )
        except rate_limit_error as exc:
            raise LogSummaryError(
                f"Claude log summary was rate-limited ({type(exc).__name__})"
            ) from exc
        except api_status_error as exc:
            raise LogSummaryError(
                f"Claude log summary API request failed ({type(exc).__name__})"
            ) from exc
        except api_connection_error as exc:
            raise LogSummaryError(
                f"Claude log summary could not reach the API ({type(exc).__name__})"
            ) from exc

        if response.stop_reason == "refusal":
            return {"status": "declined", "text": "", "model": response.model}

        text = "".join(block.text for block in response.content if block.type == "text")
        return {"status": "suggested", "text": text, "model": response.model}


def build_log_summarizer() -> ClaudeLogSummarizer | None:
    mode = os.getenv("NETCI_LOG_SUMMARY", "").strip().lower()
    if mode in {"", "none"}:
        return None
    if mode != "claude":
        raise ValueError(f"NETCI_LOG_SUMMARY must be unset, 'none' or 'claude' (got {mode!r})")

    key_file = os.getenv("NETCI_ANTHROPIC_API_KEY_FILE", "").strip()
    if not key_file:
        raise ValueError(
            "NETCI_LOG_SUMMARY=claude requires NETCI_ANTHROPIC_API_KEY_FILE to name a file "
            "holding the Anthropic API key"
        )
    try:
        with open(key_file, encoding="utf-8") as handle:
            api_key = handle.read().strip()
    except OSError as exc:
        raise ValueError(
            f"NETCI_ANTHROPIC_API_KEY_FILE names a file that could not be read: {key_file!r}"
        ) from exc
    if not api_key:
        raise ValueError(f"NETCI_ANTHROPIC_API_KEY_FILE ({key_file!r}) is empty")

    model = os.getenv("NETCI_LOG_SUMMARY_MODEL", "").strip() or DEFAULT_MODEL
    return ClaudeLogSummarizer(api_key=api_key, model=model)
