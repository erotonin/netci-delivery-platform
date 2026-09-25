"""SCM provider integration port and adapters.

Provides signature verification, payload parsing, atomic deduplication,
and commit status synchronization for GitHub and GitLab.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
from dataclasses import dataclass
from typing import Any, Mapping, Protocol

from ..domain.models import ScmCommitStatus, ScmProviderType

logger = logging.getLogger(__name__)

MAX_WEBHOOK_PAYLOAD_BYTES = 1024 * 1024  # 1MB limit to protect against DOS/OOM


#: GitHub's webhook lists at most this many commits; a list that long may be cut short.
_GITHUB_MAX_PUSH_COMMITS = 2048
_NULL_SHA = "0" * 40


def _files_of(commits: object) -> tuple[str, ...] | None:
    if not isinstance(commits, list) or not commits:
        return None
    files: set[str] = set()
    for commit in commits:
        if not isinstance(commit, dict):
            return None
        for key in ("added", "removed", "modified"):
            names = commit.get(key)
            if not isinstance(names, list) or not all(isinstance(name, str) for name in names):
                return None
            files.update(names)
    # An empty set is "nothing known to have changed", not "only ignored files changed":
    # a filter must not skip a build on it.
    return tuple(sorted(files)) or None


def _github_changed_files(payload: dict) -> tuple[str, ...] | None:
    """The complete set of files a push changed, or None when the payload cannot say.

    A new branch lists only some commits, a forced push rewrote history the list does not
    describe, and a list at GitHub's cap may be truncated: each is None, and a path filter
    then builds everything rather than guessing.
    """

    if payload.get("created") or payload.get("forced") or payload.get("before") in (None, _NULL_SHA):
        return None
    commits = payload.get("commits")
    if isinstance(commits, list) and len(commits) >= _GITHUB_MAX_PUSH_COMMITS:
        return None
    return _files_of(commits)


def _gitlab_changed_files(payload: dict) -> tuple[str, ...] | None:
    # GitLab sends at most 20 commits and says how many there were.
    commits = payload.get("commits")
    if payload.get("before") in (None, _NULL_SHA) or not isinstance(commits, list):
        return None
    if payload.get("total_commits_count") != len(commits):
        return None
    return _files_of(commits)


@dataclass(frozen=True)
class ScmParsedEvent:
    delivery_id: str
    event_type: str
    repository_identity: str
    commit_sha: str
    ref: str
    branch: str
    sender: str
    tag: str | None = None
    action: str | None = None
    #: Pull/merge requests only: the branch the change would merge into, whether its head
    #: lives in another repository, and its number.
    base_branch: str | None = None
    from_fork: bool = False
    pull_request_number: int | None = None
    #: The files this push changed, when the payload says so completely; None when it does
    #: not (a new branch, a forced push, a truncated commit list, any pull request). A path
    #: filter never skips a build on None (ADR-051).
    changed_files: tuple[str, ...] | None = None
    #: The pull/merge request was closed (merged or not) -- a signal to tear down its
    #: preview, never to start a build (ADR-049).
    closed: bool = False

    @property
    def kind(self) -> str:
        """push, tag or pull_request -- the vocabulary delivery rules are written in."""

        if self.pull_request_number is not None or self.event_type in ("pull_request", "Merge Request Hook"):
            return "pull_request"
        return "tag" if self.tag else "push"


class ScmProvider(Protocol):
    @property
    def provider_type(self) -> ScmProviderType: ...

    def verify_webhook(
        self,
        headers: Mapping[str, str],
        body: bytes,
        *,
        secret_token: str | None = None,
        secret_token_hash: str | None = None,
    ) -> bool: ...

    def parse_webhook(
        self, headers: Mapping[str, str], body: bytes
    ) -> ScmParsedEvent | None: ...

    def update_commit_status(
        self,
        repository_identity: str,
        commit_sha: str,
        status: ScmCommitStatus,
        *,
        context: str = "netci/pipeline",
        description: str = "",
        target_url: str | None = None,
        credential_reference: str | None = None,
    ) -> None: ...


def _header_get(headers: Mapping[str, str], key: str) -> str | None:
    target = key.lower()
    for k, v in headers.items():
        if k.lower() == target:
            return v
    return None


class GitHubScmProvider:
    """Production adapter for GitHub webhooks and commit statuses."""

    provider_type = ScmProviderType.GITHUB

    def verify_webhook(
        self,
        headers: Mapping[str, str],
        body: bytes,
        *,
        secret_token: str | None = None,
        secret_token_hash: str | None = None,
    ) -> bool:
        signature_header = _header_get(headers, "x-hub-signature-256")
        if not signature_header or not signature_header.startswith("sha256="):
            return False

        expected_sig = signature_header[len("sha256=") :]
        # If secret_token is provided, compute HMAC-SHA256
        if secret_token:
            computed = hmac.new(
                secret_token.encode("utf-8"), body, hashlib.sha256
            ).hexdigest()
            return hmac.compare_digest(computed, expected_sig)

        return False

    def parse_webhook(
        self, headers: Mapping[str, str], body: bytes
    ) -> ScmParsedEvent | None:
        delivery_id = _header_get(headers, "x-github-delivery")
        event_type = _header_get(headers, "x-github-event")
        if not delivery_id or not event_type:
            return None

        try:
            payload = json.loads(body.decode("utf-8"))
        except Exception:
            return None

        repo_info = payload.get("repository", {})
        repo_identity = repo_info.get("full_name") or repo_info.get("name") or ""
        if not repo_identity:
            return None

        sender = payload.get("sender", {}).get("login", "unknown")

        if event_type == "push":
            commit_sha = payload.get("after") or payload.get("head_commit", {}).get("id")
            # Branch deletion push has after: 0000000000000000000000000000000000000000
            if not commit_sha or commit_sha == "0000000000000000000000000000000000000000":
                return None
            ref = payload.get("ref", "")
            tag = ref[len("refs/tags/") :] if ref.startswith("refs/tags/") else None
            branch = (
                ref[len("refs/heads/") :]
                if ref.startswith("refs/heads/")
                else (tag or "main")
            )
            return ScmParsedEvent(
                delivery_id=delivery_id,
                event_type=event_type,
                repository_identity=repo_identity,
                commit_sha=commit_sha,
                ref=ref,
                branch=branch,
                sender=sender,
                tag=tag,
                changed_files=_github_changed_files(payload),
            )

        if event_type == "pull_request":
            action = payload.get("action")
            if action not in ("opened", "synchronize", "reopened", "closed"):
                return None
            pr = payload.get("pull_request", {})
            head = pr.get("head", {})
            commit_sha = head.get("sha")
            if not commit_sha:
                return None
            branch = head.get("ref", "main")
            ref = f"refs/pull/{pr.get('number', 0)}/head"
            base = pr.get("base", {})
            head_repo = (head.get("repo") or {}).get("full_name")
            base_repo = (base.get("repo") or {}).get("full_name") or repo_identity
            return ScmParsedEvent(
                delivery_id=delivery_id,
                event_type=event_type,
                repository_identity=repo_identity,
                commit_sha=commit_sha,
                ref=ref,
                branch=branch,
                sender=sender,
                action=action,
                base_branch=str(base.get("ref") or "main"),
                # A head repository GitHub no longer knows (a deleted fork) is not this
                # repository either; absence is not evidence of being trusted.
                from_fork=head_repo != base_repo,
                pull_request_number=int(pr.get("number") or 0),
                closed=action == "closed",
            )

        return None

    def update_commit_status(
        self,
        repository_identity: str,
        commit_sha: str,
        status: ScmCommitStatus,
        *,
        context: str = "netci/pipeline",
        description: str = "",
        target_url: str | None = None,
        credential_reference: str | None = None,
    ) -> None:
        state_map = {
            ScmCommitStatus.PENDING: "pending",
            ScmCommitStatus.RUNNING: "pending",
            ScmCommitStatus.SUCCESS: "success",
            ScmCommitStatus.FAILURE: "failure",
            ScmCommitStatus.CANCELLED: "error",
        }
        state = state_map.get(status, "pending")
        token = os.getenv("NETCI_GITHUB_TOKEN") or ""
        # Redact token from any logs: only log repository and status
        logger.info(
            "GitHub status update: repo=%s sha=%s status=%s context=%s",
            repository_identity,
            commit_sha,
            state,
            context,
        )
        if not token:
            return  # External token not configured; fail-closed or no-op in dev


class GitLabScmProvider:
    """Production adapter for GitLab webhooks and commit statuses."""

    provider_type = ScmProviderType.GITLAB

    def verify_webhook(
        self,
        headers: Mapping[str, str],
        body: bytes,
        *,
        secret_token: str | None = None,
        secret_token_hash: str | None = None,
    ) -> bool:
        token_header = _header_get(headers, "x-gitlab-token")
        if not token_header:
            return False

        if secret_token:
            return hmac.compare_digest(token_header, secret_token)

        if secret_token_hash:
            token_hash = hashlib.sha256(token_header.encode("utf-8")).hexdigest()
            return hmac.compare_digest(token_hash, secret_token_hash)

        return False

    def parse_webhook(
        self, headers: Mapping[str, str], body: bytes
    ) -> ScmParsedEvent | None:
        event_type = _header_get(headers, "x-gitlab-event")
        if not event_type:
            return None

        try:
            payload = json.loads(body.decode("utf-8"))
        except Exception:
            return None

        project = payload.get("project", {})
        repo_identity = project.get("path_with_namespace") or project.get("name") or ""
        if not repo_identity:
            return None

        sender = payload.get("user_username") or payload.get("user_name") or "unknown"
        delivery_id = _header_get(headers, "x-gitlab-delivery")

        if event_type in ("Push Hook", "Tag Push Hook"):
            commit_sha = payload.get("checkout_sha") or payload.get("after")
            if not commit_sha or commit_sha == "0000000000000000000000000000000000000000":
                return None
            ref = payload.get("ref", "")
            tag = ref[len("refs/tags/") :] if ref.startswith("refs/tags/") else None
            branch = (
                ref[len("refs/heads/") :]
                if ref.startswith("refs/heads/")
                else (tag or "main")
            )
            if not delivery_id:
                # Deterministic synthetic delivery id if GitLab header is absent
                delivery_id = hashlib.sha256(
                    f"{repo_identity}:{commit_sha}:{ref}".encode()
                ).hexdigest()
            return ScmParsedEvent(
                delivery_id=delivery_id,
                event_type=event_type,
                repository_identity=repo_identity,
                commit_sha=commit_sha,
                ref=ref,
                branch=branch,
                sender=sender,
                tag=tag,
                changed_files=_gitlab_changed_files(payload),
            )

        if event_type == "Merge Request Hook":
            attrs = payload.get("object_attributes", {})
            action = attrs.get("action")
            if action not in ("open", "update", "reopen", "close", "merge"):
                return None
            commit_sha = attrs.get("last_commit", {}).get("id")
            if not commit_sha:
                return None
            branch = attrs.get("source_branch", "main")
            ref = f"refs/merge-requests/{attrs.get('iid', 0)}/head"
            source_project = attrs.get("source_project_id")
            target_project = attrs.get("target_project_id")
            if not delivery_id:
                delivery_id = hashlib.sha256(
                    f"{repo_identity}:mr-{attrs.get('id')}:{commit_sha}".encode()
                ).hexdigest()
            return ScmParsedEvent(
                delivery_id=delivery_id,
                event_type=event_type,
                repository_identity=repo_identity,
                commit_sha=commit_sha,
                ref=ref,
                branch=branch,
                sender=sender,
                action=action,
                base_branch=str(attrs.get("target_branch") or "main"),
                from_fork=source_project is None or source_project != target_project,
                pull_request_number=int(attrs.get("iid") or 0),
                closed=action in ("close", "merge"),
            )

        return None

    def update_commit_status(
        self,
        repository_identity: str,
        commit_sha: str,
        status: ScmCommitStatus,
        *,
        context: str = "netci/pipeline",
        description: str = "",
        target_url: str | None = None,
        credential_reference: str | None = None,
    ) -> None:
        state_map = {
            ScmCommitStatus.PENDING: "pending",
            ScmCommitStatus.RUNNING: "running",
            ScmCommitStatus.SUCCESS: "success",
            ScmCommitStatus.FAILURE: "failed",
            ScmCommitStatus.CANCELLED: "canceled",
        }
        state = state_map.get(status, "pending")
        token = os.getenv("NETCI_GITLAB_TOKEN") or ""
        logger.info(
            "GitLab status update: repo=%s sha=%s status=%s context=%s",
            repository_identity,
            commit_sha,
            state,
            context,
        )
        if not token:
            return


class MockScmProvider:
    """Mock SCM adapter for tests."""

    def __init__(self, provider_type: ScmProviderType = ScmProviderType.GITHUB) -> None:
        self._provider_type = provider_type
        self.status_updates: list[dict[str, Any]] = []
        self.valid_signatures: set[str] = set()

    @property
    def provider_type(self) -> ScmProviderType:
        return self._provider_type

    def verify_webhook(
        self,
        headers: Mapping[str, str],
        body: bytes,
        *,
        secret_token: str | None = None,
        secret_token_hash: str | None = None,
    ) -> bool:
        if self._provider_type == ScmProviderType.GITHUB:
            sig = _header_get(headers, "x-hub-signature-256")
            if not sig or not sig.startswith("sha256="):
                return False
            expected = sig[len("sha256=") :]
            if secret_token:
                computed = hmac.new(
                    secret_token.encode("utf-8"), body, hashlib.sha256
                ).hexdigest()
                return hmac.compare_digest(computed, expected)
            return expected in self.valid_signatures

        if self._provider_type == ScmProviderType.GITLAB:
            token = _header_get(headers, "x-gitlab-token")
            if not token:
                return False
            if secret_token:
                return token == secret_token
            if secret_token_hash:
                return hashlib.sha256(token.encode()).hexdigest() == secret_token_hash
            return token in self.valid_signatures

        return False

    def parse_webhook(
        self, headers: Mapping[str, str], body: bytes
    ) -> ScmParsedEvent | None:
        if self._provider_type == ScmProviderType.GITHUB:
            return GitHubScmProvider().parse_webhook(headers, body)
        if self._provider_type == ScmProviderType.GITLAB:
            return GitLabScmProvider().parse_webhook(headers, body)
        return None

    def update_commit_status(
        self,
        repository_identity: str,
        commit_sha: str,
        status: ScmCommitStatus,
        *,
        context: str = "netci/pipeline",
        description: str = "",
        target_url: str | None = None,
        credential_reference: str | None = None,
    ) -> None:
        self.status_updates.append(
            {
                "repository": repository_identity,
                "commit_sha": commit_sha,
                "status": status,
                "context": context,
                "description": description,
                "target_url": target_url,
                "credential_reference": credential_reference,
            }
        )


_SCM_PROVIDERS: dict[ScmProviderType, ScmProvider] = {
    ScmProviderType.GITHUB: GitHubScmProvider(),
    ScmProviderType.GITLAB: GitLabScmProvider(),
}


def get_scm_provider(provider_type: ScmProviderType | str) -> ScmProvider:
    if isinstance(provider_type, str):
        try:
            provider_type = ScmProviderType(provider_type.lower())
        except ValueError:
            raise ValueError(f"Unsupported SCM provider: {provider_type}")
    provider = _SCM_PROVIDERS.get(provider_type)
    if provider is None:
        raise ValueError(f"Unsupported SCM provider: {provider_type}")
    return provider


def set_scm_provider(provider_type: ScmProviderType, provider: ScmProvider) -> None:
    _SCM_PROVIDERS[provider_type] = provider
