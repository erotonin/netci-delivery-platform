"""Ephemeral Preview Environments domain logic (ADR-049).

A preview used to be a row: `create_preview` wrote "active" with a URL nobody served and
deployed nothing, which is exactly the fake-success this project forbids. A preview is now
a deployment of a pull request's published digest -- it is `deploying` until the worker
reports, its `active` URL is only what the worker read back from the cluster, and the same
pull request pushed again redeploys the same row into the same namespace rather than
minting a new one.

This module owns the row's transitions. It does not talk to Temporal: `PreviewRequest` is
what the caller (the Portal, which knows the module's deployment target) must start once
its own transaction commits -- mirroring `DeliveryPlatform._start_cd`.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from ..errors import ApiError
from ..store.records import PreviewEnvironmentRecord
from ..store.session import PlatformSession


MIN_TTL_HOURS = 1
DEFAULT_TTL_HOURS = 24
MAX_TTL_HOURS = 72

#: Written as the `detail` of a "destroying" row the reaper (not a person or a PR close)
#: moved there. `record_result` reads this back to tell an expiry's "destroyed" report
#: from any other teardown's, because the status column has no third value for "why".
EXPIRY_DETAIL_PREFIX = "ttl expired"

#: What a worker's result may transition, keyed by the status it found the row in.
#: Anything else -- a stale retry, a forged report, a row already gone -- is refused.
_RESULT_TRANSITIONS: dict[str, frozenset[str]] = {
    "deploying": frozenset({"active", "failed"}),
    "destroying": frozenset({"destroyed", "failed"}),
}


class PreviewEnvironmentError(ApiError):
    """A preview operation is refused; the client gets this code and status."""


@dataclass(frozen=True)
class PreviewRequest:
    """What must be started once the transaction that wrote this row has committed.

    Built from a `PreviewEnvironmentRecord` plus whatever the module's deployment target
    supplies (`kubeconfig_ref`, `image_pull_host`) -- the row itself carries none of that,
    so a preview never has to be re-derived from stale copies of it.
    """

    preview_id: str
    application_id: UUID
    pipeline_run_id: UUID
    action: str  # "deploy" | "teardown"
    namespace: str
    release: str
    artifact_digest: str = ""
    parameters: dict[str, Any] = field(default_factory=dict)


def _slug(module_id: str) -> str:
    """The module id, bounded to fit the playbook's namespace/release regex.

    `preview-<module>-pr-<n>` must stay within Kubernetes' 63-character namespace limit
    and match `preview-[a-z0-9]([a-z0-9-]{0,38}[a-z0-9])?-pr-[0-9]{1,9}`, so the module
    segment is capped at 40 characters and must not end on a hyphen the cut could leave.
    When module_id exceeds 40 characters, a deterministic hash suffix is appended so that
    different modules sharing a common prefix never collide on the same namespace/release.
    """

    clean = re.sub(r"[^a-z0-9-]", "-", module_id.lower()).strip("-")
    if not clean:
        clean = "module"
    if len(clean) <= 40:
        return clean.rstrip("-")
    digest = hashlib.sha256(module_id.encode("utf-8")).hexdigest()[:6]
    return f"{clean[:33].rstrip('-')}-{digest}"


def preview_names(module_id: str, pull_request_number: int) -> tuple[str, str]:
    """(namespace, release) for one module's preview of one pull request -- deterministic,
    so the same pull request pushed again finds and redeploys the same row."""

    release = f"{_slug(module_id)}-pr-{pull_request_number}"
    return f"preview-{release}", release


class PreviewEnvironmentManager:
    """Write and transition preview rows within the caller's transaction."""

    def __init__(self, session: PlatformSession) -> None:
        self._session = session

    def request(
        self,
        *,
        application_id: UUID,
        module_id: str,
        pull_request_id: str,
        pull_request_number: int,
        pipeline_run_id: UUID,
        commit_sha: str,
        artifact_digest: str,
        ttl_hours: int,
        created_by: str,
    ) -> PreviewEnvironmentRecord:
        """Write the "deploying" row for this module's pull request, or refresh it.

        The row's id is the release name, deterministic from the module and the pull
        request number -- a second push finds the same row and redeploys the same
        namespace instead of leaking a new one for every commit.
        """

        namespace, release = preview_names(module_id, pull_request_number)
        now = datetime.now(timezone.utc)
        ttl_seconds = max(MIN_TTL_HOURS, min(ttl_hours, MAX_TTL_HOURS)) * 3600
        expires_at = now + timedelta(seconds=ttl_seconds)
        existing = self._session.preview_environment(release)
        if existing is None:
            record = PreviewEnvironmentRecord(
                id=release,
                application_id=application_id,
                pull_request_id=pull_request_id,
                commit_sha=commit_sha,
                namespace=namespace,
                url=None,
                status="deploying",
                ttl_seconds=ttl_seconds,
                expires_at=expires_at,
                created_by=created_by,
                created_at=now,
                pipeline_run_id=pipeline_run_id,
                artifact_digest=artifact_digest,
                release_name=release,
                detail="",
            )
            self._session.insert_preview_environment(record)
            return record
        updated = self._session.update_preview_environment(
            release,
            status="deploying",
            detail="",
            url=None,
            artifact_digest=artifact_digest,
            pipeline_run_id=pipeline_run_id,
            expires_at=expires_at,
            commit_sha=commit_sha,
        )
        if updated is None:
            raise PreviewEnvironmentError(
                "PREVIEW_UPDATE_FAILED", f"failed to update preview environment '{release}'", 500
            )
        return updated

    def record_result(
        self, preview_id: str, *, status: str, message: str = "", url: str | None = None
    ) -> PreviewEnvironmentRecord:
        """Apply a worker's deploy or teardown result.

        `active` never gets a url this did not carry, and a status the transition table
        does not allow -- a stale retry, a replayed report, a row already gone -- is a 409,
        not a silent overwrite of whatever is there.
        """

        current = self._session.preview_environment(preview_id)
        if current is None:
            raise PreviewEnvironmentError(
                "PREVIEW_NOT_FOUND", f"preview environment '{preview_id}' not found", 404
            )
        if current.pipeline_run_id is None:
            raise PreviewEnvironmentError(
                "PREVIEW_NOT_STARTED",
                f"preview environment '{preview_id}' was never deployed by a worker",
                409,
            )
        allowed = _RESULT_TRANSITIONS.get(current.status, frozenset())
        if status not in allowed:
            raise PreviewEnvironmentError(
                "INVALID_PREVIEW_STATE",
                f"a preview cannot go from {current.status} to {status}",
                409,
            )
        # The status column has no "expired" transition of its own -- the reaper started
        # this teardown for TTL, not for a person or a closed pull request, and that is
        # recorded nowhere else. `destroyed` from a teardown the reaper started is `expired`.
        final_status = "expired" if status == "destroyed" and current.detail.startswith(EXPIRY_DETAIL_PREFIX) else status
        now = datetime.now(timezone.utc)
        updated = self._session.update_preview_environment(
            preview_id,
            status=final_status,
            detail=message,
            url=url,
            destroyed_at=now if final_status in ("destroyed", "expired") else None,
            expected_status=current.status,
        )
        if updated is None:
            raise PreviewEnvironmentError(
                "INVALID_PREVIEW_STATE",
                f"preview environment '{preview_id}' was concurrently modified",
                409,
            )
        return updated

    def start_teardown(self, preview_id: str, *, detail: str) -> PreviewEnvironmentRecord | None:
        """Move an active or deploying preview to `destroying`.

        None when the row does not exist or is already gone -- teardown is not something
        a row in `failed`, `destroyed`, `expired` or already `destroying` can be asked
        for again through this path.
        """

        current = self._session.preview_environment(preview_id)
        if current is None or current.status not in ("active", "deploying"):
            return None
        return self._session.update_preview_environment(
            preview_id,
            status="destroying",
            detail=detail,
            expected_status=("active", "deploying"),
        )

    def reconcile_expiry(self, now: datetime | None = None) -> list[PreviewEnvironmentRecord]:
        """Move every preview whose TTL has passed to `destroying`."""

        check_time = now or datetime.now(timezone.utc)
        reconciled: list[PreviewEnvironmentRecord] = []
        for expired in self._session.expired_preview_environments(check_time):
            updated = self._session.update_preview_environment(
                expired.id,
                status="destroying",
                detail=f"{EXPIRY_DETAIL_PREFIX} at {expired.expires_at.isoformat()}",
                expected_status=("active", "deploying"),
            )
            if updated is not None:
                reconciled.append(updated)
        return reconciled
