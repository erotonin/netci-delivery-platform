"""Delivers outbox notifications to GitHub / GitLab: commit statuses and PR/MR comments.

`delivery.py` enqueues these as `NotificationRecord` rows (event_type
"scm.commit_status" / "scm.pr_comment", recipient "scm") rather than calling the
provider inline, so a slow or down GitHub/GitLab never blocks a pipeline transition.
This module is the other half: what the outbox worker actually calls to make that
status visible on the commit or PR. Every failure path here must raise
`ScmReportingError` -- returning normally is what the outbox reads as "delivered",
and a status GitHub never received but netCI believes it sent is exactly the kind of
unverified green screen this project refuses to show (see CLAUDE.md non-negotiable #1).
"""

from __future__ import annotations

import logging
import os
import re
import urllib.parse
from typing import Any

import httpx

from ..domain.models import NotificationRecord

logger = logging.getLogger(__name__)

# The provider APIs reject anything outside this shape anyway, but validating here
# means a malformed payload fails fast with a clear reason instead of as an opaque
# 404/422 from GitHub/GitLab -- and never as a request to some unintended URL built
# from unchecked input.
_REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+(/[A-Za-z0-9_.-]+)+$")
_COMMIT_SHA_RE = re.compile(r"^[0-9a-f]{7,64}$")

# netCI's own pipeline states don't map 1:1 onto either provider's vocabulary
# (GitHub has no "running", GitLab spells "failure" as "failed").
_GITHUB_STATE_MAP = {
    "pending": "pending",
    "running": "pending",
    "success": "success",
    "failure": "failure",
    "cancelled": "error",
}
_GITLAB_STATE_MAP = {
    "pending": "pending",
    "running": "running",
    "success": "success",
    "failure": "failed",
    "cancelled": "canceled",
}


class ScmReportingError(RuntimeError):
    """Raised for any refusal or delivery failure.

    Raising -- never returning -- is what makes the outbox worker retry with
    backoff and eventually dead-letter the notification, instead of silently
    treating an unreported commit status as delivered.
    """


def _read_secret(name: str) -> str:
    """Prefer a mounted secret file (`<name>_FILE`) over an inline env value.

    Same convention as `adapters/jenkins_http.py`: a token handed in via env var
    sits in `/proc/<pid>/environ` and process listings for the life of the
    process, so anything that can be mounted as a file should be.
    """
    path = os.getenv(f"{name}_FILE", "").strip()
    if path:
        with open(path, encoding="utf-8") as handle:
            return handle.read().strip()
    return os.getenv(name, "").strip()


class ScmReporter:
    """Notification dispatcher for `scm.commit_status` / `scm.pr_comment` outbox rows."""

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        # Tests inject a MockTransport here instead of hitting the network.
        self._transport = transport
        self._github_token = _read_secret("NETCI_GITHUB_TOKEN")
        self._github_api_url = os.getenv("NETCI_GITHUB_API_URL", "https://api.github.com").rstrip("/")
        self._gitlab_token = _read_secret("NETCI_GITLAB_TOKEN")
        self._gitlab_url = os.getenv("NETCI_GITLAB_URL", "https://gitlab.com").rstrip("/")

    async def dispatch(self, notification: NotificationRecord) -> None:
        payload = notification.payload
        provider = payload.get("provider")
        repository = payload.get("repository")
        self._validate_repository(repository)

        if notification.event_type == "scm.commit_status":
            commit_sha = payload.get("commitSha")
            self._validate_commit_sha(commit_sha)
            state = payload.get("state")
            context = payload.get("context")
            description = payload.get("description")
            target_url = payload.get("targetUrl")

            if provider == "github":
                await self._github_commit_status(repository, commit_sha, state, context, description, target_url)
            elif provider == "gitlab":
                await self._gitlab_commit_status(repository, commit_sha, state, context, description, target_url)
            else:
                raise ScmReportingError(f"unknown SCM provider: {provider!r}")

            logger.info(
                "scm status delivered provider=%s repository=%s event=%s state=%s",
                provider,
                repository,
                notification.event_type,
                state,
            )
        elif notification.event_type == "scm.pr_comment":
            pull_request = payload.get("pullRequest")
            self._validate_pull_request(pull_request)
            body = payload.get("body", "")

            if provider == "github":
                await self._github_pr_comment(repository, pull_request, body)
            elif provider == "gitlab":
                await self._gitlab_mr_comment(repository, pull_request, body)
            else:
                raise ScmReportingError(f"unknown SCM provider: {provider!r}")

            logger.info(
                "scm comment delivered provider=%s repository=%s event=%s pr=%s",
                provider,
                repository,
                notification.event_type,
                pull_request,
            )
        else:
            raise ScmReportingError(f"unknown SCM notification event type: {notification.event_type!r}")

    # -- validation -----------------------------------------------------------------

    @staticmethod
    def _validate_repository(repository: Any) -> None:
        if not isinstance(repository, str) or not _REPOSITORY_RE.match(repository):
            raise ScmReportingError(f"invalid SCM repository identifier: {repository!r}")

    @staticmethod
    def _validate_commit_sha(commit_sha: Any) -> None:
        if not isinstance(commit_sha, str) or not _COMMIT_SHA_RE.match(commit_sha):
            raise ScmReportingError(f"invalid commit sha: {commit_sha!r}")

    @staticmethod
    def _validate_pull_request(pull_request: Any) -> None:
        # bool is a subclass of int in Python; True/False are not pull request numbers.
        if isinstance(pull_request, bool) or not isinstance(pull_request, int) or pull_request <= 0:
            raise ScmReportingError(f"invalid pull request number: {pull_request!r}")

    # -- GitHub -----------------------------------------------------------------

    def _github_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._github_token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    async def _github_commit_status(
        self,
        repository: str,
        commit_sha: str,
        state: Any,
        context: Any,
        description: Any,
        target_url: Any,
    ) -> None:
        if not self._github_token:
            raise ScmReportingError("NETCI_GITHUB_TOKEN_FILE is not configured; the status cannot be sent")
        mapped_state = _GITHUB_STATE_MAP.get(state)
        if mapped_state is None:
            raise ScmReportingError(f"unknown commit status state: {state!r}")

        body: dict[str, Any] = {
            "state": mapped_state,
            "context": context,
            "description": (description or "")[:140],
        }
        if target_url is not None:
            body["target_url"] = target_url

        url = f"{self._github_api_url}/repos/{repository}/statuses/{commit_sha}"
        await self._post("github", url, self._github_headers(), body)

    async def _github_pr_comment(self, repository: str, pull_request: int, body: Any) -> None:
        if not self._github_token:
            raise ScmReportingError("NETCI_GITHUB_TOKEN_FILE is not configured; the comment cannot be sent")

        url = f"{self._github_api_url}/repos/{repository}/issues/{pull_request}/comments"
        await self._post("github", url, self._github_headers(), {"body": body})

    # -- GitLab -----------------------------------------------------------------

    def _gitlab_headers(self) -> dict[str, str]:
        return {"PRIVATE-TOKEN": self._gitlab_token}

    async def _gitlab_commit_status(
        self,
        repository: str,
        commit_sha: str,
        state: Any,
        context: Any,
        description: Any,
        target_url: Any,
    ) -> None:
        if not self._gitlab_token:
            raise ScmReportingError("NETCI_GITLAB_TOKEN_FILE is not configured; the status cannot be sent")
        mapped_state = _GITLAB_STATE_MAP.get(state)
        if mapped_state is None:
            raise ScmReportingError(f"unknown commit status state: {state!r}")

        body: dict[str, Any] = {
            "state": mapped_state,
            "name": context,
            "description": description,
        }
        if target_url is not None:
            body["target_url"] = target_url

        quoted_repository = urllib.parse.quote(repository, safe="")
        url = f"{self._gitlab_url}/api/v4/projects/{quoted_repository}/statuses/{commit_sha}"
        await self._post("gitlab", url, self._gitlab_headers(), body)

    async def _gitlab_mr_comment(self, repository: str, pull_request: int, body: Any) -> None:
        if not self._gitlab_token:
            raise ScmReportingError("NETCI_GITLAB_TOKEN_FILE is not configured; the comment cannot be sent")

        quoted_repository = urllib.parse.quote(repository, safe="")
        url = f"{self._gitlab_url}/api/v4/projects/{quoted_repository}/merge_requests/{pull_request}/notes"
        await self._post("gitlab", url, self._gitlab_headers(), {"body": body})

    # -- transport -----------------------------------------------------------------

    async def _post(self, provider: str, url: str, headers: dict[str, str], body: dict[str, Any]) -> None:
        async with httpx.AsyncClient(timeout=10.0, transport=self._transport) as client:
            try:
                response = await client.post(url, headers=headers, json=body)
            except httpx.HTTPError as exc:
                # exc's own message may include the URL but httpx never puts headers
                # (i.e. the token) into it -- safe to include verbatim.
                raise ScmReportingError(f"{provider}: request to {url} failed: {exc}") from exc

        if response.status_code >= 300:
            # Truncated response body only -- never the request headers, so a
            # provider that echoes nothing back still can't leak the token here.
            raise ScmReportingError(
                f"{provider} responded {response.status_code} to {url}: {response.text[:300]}"
            )
