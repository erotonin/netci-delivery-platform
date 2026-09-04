"""Values shared between the domain and every store implementation.

The stores themselves live in `app.store`. This module holds only what both sides must
agree on -- how a connection string is resolved, what an audit record and an idempotency
record are, what one atomic unit of work contains, and the two failures a caller has to
tell apart -- so neither `app.store.postgres` nor `app.store.memory` has to import the
other to define them.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from .domain.models import Application, DeliveryEvent, Deployment, PipelineRun

logger = logging.getLogger(__name__)


def database_url() -> str:
    """Resolve the connection string, preferring a mounted secret file.

    Keeping the credential out of the process environment is the first step of the
    P1 secret-handling item; a compose/Kubernetes secret can be mounted instead.
    """

    path = os.getenv("DATABASE_URL_FILE", "").strip()
    if path:
        try:
            with open(path, encoding="utf-8") as handle:
                return handle.read().strip()
        except OSError as exc:
            logger.error("DATABASE_URL_FILE is unreadable: %s", exc)
            return ""
    return os.getenv("DATABASE_URL", "").strip()


@dataclass(frozen=True)
class AuditRecord:
    event_type: str
    application_id: UUID | None = None
    pipeline_run_id: UUID | None = None
    deployment_id: UUID | None = None
    actor: str | None = None
    correlation_id: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    id: UUID = field(default_factory=uuid4)
    occurred_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass(frozen=True)
class IdempotencyRow:
    scope: str
    idempotency_key: str
    request_hash: str
    resource_type: str
    resource_id: UUID
    response_status: int


@dataclass
class UnitOfWork:
    """One delivery state change plus every fact it must publish atomically.

    Writing state, outbox events, audit and logs through a single transaction is
    what stops "state changed but the DORA event was lost" from being possible.
    Each run/deployment carries the version it was read at, so two concurrent
    callbacks cannot both win.
    """

    applications: list[Application] = field(default_factory=list)
    runs: list[tuple[PipelineRun, int | None]] = field(default_factory=list)
    deployments: list[tuple[Deployment, int | None]] = field(default_factory=list)
    events: list[DeliveryEvent] = field(default_factory=list)
    audit: list[AuditRecord] = field(default_factory=list)
    logs: list[tuple[UUID, list[str]]] = field(default_factory=list)
    security_evidence: list[tuple[UUID, UUID, str, dict[str, Any]]] = field(default_factory=list)
    idempotency: list[IdempotencyRow] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not any(
            (
                self.applications,
                self.runs,
                self.deployments,
                self.events,
                self.audit,
                self.logs,
                self.security_evidence,
                self.idempotency,
            )
        )


class ConcurrentModification(RuntimeError):
    """Raised when a record changed between read and write."""


class StillReferenced(RuntimeError):
    """Raised when a row cannot be deleted because something still points at it.

    Distinct from a storage failure, because the caller should say something different:
    "the database is down" sends an operator to the wrong place when the truth is "a
    production request still names this module".
    """
