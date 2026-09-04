"""Row shapes the Portal hierarchy is read and written as.

These are deliberately plain: the Portal service owns the response shapes, and the
store owns durability. Keeping the boundary at a dataclass stops SQL column names
from leaking into the API layer and stops response formatting from leaking into SQL.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID


@dataclass(frozen=True)
class SystemRow:
    id: str
    unit: str
    description: str
    owner: str
    status: str


@dataclass(frozen=True)
class ModuleRow:
    id: str
    system_id: str
    name: str
    module_type: str
    description: str
    runtime: str
    application_id: UUID | None = None
    deployment_config: list[dict[str, Any]] = field(default_factory=list)
    pipeline_config: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class VersionRow:
    module_id: str
    version: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RequestModuleRow:
    module_id: str
    version: str
    deployment_order: int = 1


@dataclass(frozen=True)
class RequestRow:
    id: str
    modules: tuple[RequestModuleRow, ...]
    requested_by: str
    scheduled_for: datetime
    rollback_strategy: str
    run_automation_tests: bool
    status: str
    deployment_id: UUID | None = None
    comment: str | None = None
    idempotency_key: str | None = None
    request_hash: str | None = None


@dataclass(frozen=True)
class DeploymentLease:
    """Exclusive claim on one deployment target, held for a bounded time.

    The lease is what stops two workflows writing to the same hosts. The fencing token is
    what stops the loser writing *afterwards*: it rises every time the target is claimed,
    every callback carries the one its deployment was started under, and a lower one is
    refused. Without it, a workflow that paused past its expiry could come back and
    overwrite the result of the workflow that replaced it.
    """

    id: UUID
    application_id: UUID
    environment: str
    target: str
    deployment_id: UUID
    owner: str
    fencing_token: int
    acquired_at: datetime
    heartbeat_at: datetime
    expires_at: datetime
    released_at: datetime | None = None
    release_reason: str | None = None

    def is_expired(self, now: datetime) -> bool:
        return self.released_at is None and self.expires_at <= now
