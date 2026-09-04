"""Break-glass emergency bypass service with dual-control authorization and bounded leases."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from ..store.records import BreakGlassRecord
from ..store.session import PlatformSession
from .rules import PolicyViolation


class BreakGlassError(PolicyViolation):
    """Raised when a break-glass request or approval violates dual control or constraints."""
    pass


class BreakGlassService:
    """Manages emergency override procedures with two-person separation of duties."""

    MAX_TTL_MINUTES = 240  # 4 hours max
    DEFAULT_TTL_MINUTES = 60  # 1 hour default

    @classmethod
    def create_request(
        cls,
        session: PlatformSession,
        *,
        target_type: str,
        target_id: str,
        requested_by: str,
        reason: str,
        incident_ticket: str,
    ) -> BreakGlassRecord:
        if not requested_by or not requested_by.strip():
            raise BreakGlassError("Break-glass request requires an authenticated requester")
        if not reason or not reason.strip():
            raise BreakGlassError("Break-glass request requires an explicit justification reason")
        if not incident_ticket or not incident_ticket.strip():
            raise BreakGlassError("Break-glass request requires an associated incident ticket")

        record = BreakGlassRecord(
            id=uuid4(),
            target_type=target_type.strip(),
            target_id=target_id.strip(),
            requested_by=requested_by.strip(),
            reason=reason.strip(),
            incident_ticket=incident_ticket.strip(),
            status="pending",
            created_at=datetime.now(timezone.utc),
        )
        session.insert_break_glass_request(record)
        return record

    @classmethod
    def approve_request(
        cls,
        session: PlatformSession,
        *,
        request_id: UUID,
        approved_by: str,
        ttl_minutes: int = DEFAULT_TTL_MINUTES,
    ) -> BreakGlassRecord:
        req = session.break_glass_request(request_id)
        if not req:
            raise BreakGlassError(f"Break-glass request {request_id} not found")
        if req.status != "pending":
            raise BreakGlassError(f"Break-glass request {request_id} is already {req.status}")

        if not approved_by or not approved_by.strip():
            raise BreakGlassError("Break-glass approval requires an authenticated approver")

        # Two-person dual-control rule: requester cannot approve own break-glass
        if req.requested_by == approved_by.strip():
            raise BreakGlassError(
                "Break-glass dual-control violation: the requester cannot approve their own break-glass request"
            )

        bounded_ttl = min(max(1, ttl_minutes), cls.MAX_TTL_MINUTES)
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(minutes=bounded_ttl)

        updated = session.approve_break_glass_request(
            request_id=request_id,
            approved_by=approved_by.strip(),
            approved_at=now,
            expires_at=expires_at,
        )
        if not updated:
            raise BreakGlassError(f"Failed to activate break-glass request {request_id}")
        return updated

    @classmethod
    def active_break_glass(
        cls,
        session: PlatformSession,
        *,
        target_type: str,
        target_id: str,
        now: datetime | None = None,
    ) -> BreakGlassRecord | None:
        ts = now or datetime.now(timezone.utc)
        return session.active_break_glass(target_type=target_type, target_id=target_id, now=ts)
