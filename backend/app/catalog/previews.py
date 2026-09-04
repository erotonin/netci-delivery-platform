"""Ephemeral Preview Environments domain logic."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from uuid import UUID

from ..store.records import PreviewEnvironmentRecord
from ..store.session import PlatformSession


MIN_TTL_SECONDS = 3600        # 1 hour
DEFAULT_TTL_SECONDS = 86400    # 24 hours
MAX_TTL_SECONDS = 259200      # 72 hours


class PreviewEnvironmentError(ValueError):
    """Raised when preview environment operations fail."""


class PreviewEnvironmentManager:
    """Manages creation, lifecycle, TTL enforcement, and cleanup of ephemeral preview environments."""

    def __init__(self, session: PlatformSession) -> None:
        self._session = session

    def create_preview(
        self,
        *,
        application_id: UUID,
        pull_request_id: str,
        commit_sha: str,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        created_by: str = "developer",
    ) -> PreviewEnvironmentRecord:
        app = self._session.application(application_id)
        if not app:
            raise PreviewEnvironmentError(f"application '{application_id}' does not exist")

        pr_clean = re.sub(r"[^a-zA-Z0-9-]", "-", pull_request_id.lower()).strip("-")
        if not pr_clean:
            raise PreviewEnvironmentError("pull_request_id cannot be empty or solely special characters")

        commit_sha = commit_sha.strip()
        if not commit_sha:
            raise PreviewEnvironmentError("commit_sha cannot be empty")

        bounded_ttl = max(MIN_TTL_SECONDS, min(ttl_seconds, MAX_TTL_SECONDS))
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(seconds=bounded_ttl)

        # Check existing active preview for this PR
        existing_previews = self._session.list_preview_environments(
            application_id=application_id, status="active"
        )
        for existing in existing_previews:
            if existing.pull_request_id == pull_request_id and not existing.is_expired(now):
                # Refresh existing preview environment with new commit SHA and reset TTL
                self._session.update_preview_environment_status(existing.id, status="destroyed", destroyed_at=now)
                break

        app_slug = re.sub(r"[^a-zA-Z0-9-]", "-", app.name.lower())[:16].strip("-")
        preview_id = f"prv-{app_slug}-{pr_clean[:12]}"
        namespace = f"netci-{preview_id}"
        url = f"https://{preview_id}.preview.netci.internal"

        record = PreviewEnvironmentRecord(
            id=preview_id,
            application_id=application_id,
            pull_request_id=pull_request_id,
            commit_sha=commit_sha,
            namespace=namespace,
            url=url,
            status="active",
            ttl_seconds=bounded_ttl,
            expires_at=expires_at,
            created_by=created_by.strip(),
            created_at=now,
        )
        self._session.insert_preview_environment(record)
        return record

    def teardown_preview(self, preview_id: str) -> PreviewEnvironmentRecord:
        current = self._session.preview_environment(preview_id)
        if not current:
            raise PreviewEnvironmentError(f"preview environment '{preview_id}' not found")

        now = datetime.now(timezone.utc)
        updated = self._session.update_preview_environment_status(
            preview_id, status="destroyed", destroyed_at=now
        )
        if not updated:
            raise PreviewEnvironmentError(f"failed to update preview environment '{preview_id}'")
        return updated

    def reconcile_expiry(self, now: datetime | None = None) -> list[PreviewEnvironmentRecord]:
        check_time = now or datetime.now(timezone.utc)
        expired_records = self._session.expired_preview_environments(check_time)
        reconciled: list[PreviewEnvironmentRecord] = []

        for exp in expired_records:
            updated = self._session.update_preview_environment_status(
                exp.id, status="expired", destroyed_at=check_time
            )
            if updated:
                reconciled.append(updated)

        return reconciled
