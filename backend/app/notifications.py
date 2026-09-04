"""Transactional notification outbox pattern and asynchronous delivery worker."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol
from uuid import UUID, uuid4

import httpx

from .domain.models import NotificationRecord, NotificationStatus
from .metrics import metrics

logger = logging.getLogger(__name__)


class NotificationDispatcher(Protocol):
    async def dispatch(self, notification: NotificationRecord) -> None:
        """Dispatch notification to destination, raising on delivery failure."""
        ...


class HttpWebhookNotificationDispatcher:
    """Dispatches notifications via HTTP POST with optional HMAC signature."""

    def __init__(self, secret: str | None = None, timeout_seconds: float = 5.0) -> None:
        self.secret = secret
        self.timeout_seconds = timeout_seconds

    async def dispatch(self, notification: NotificationRecord) -> None:
        # If recipient is not an HTTP(S) URL, fall back to log
        if not (notification.recipient.startswith("http://") or notification.recipient.startswith("https://")):
            logger.info("Outbox log notification [%s]: %s -> %s", notification.event_type, notification.aggregate_id, notification.payload)
            return

        body = json.dumps(
            {
                "id": str(notification.id),
                "event_type": notification.event_type,
                "aggregate_type": notification.aggregate_type,
                "aggregate_id": notification.aggregate_id,
                "payload": notification.payload,
                "timestamp": notification.created_at.isoformat(),
            },
            default=str,
        ).encode("utf-8")

        headers = {
            "Content-Type": "application/json",
            "X-NetCI-Event": notification.event_type,
            "X-NetCI-Delivery-ID": str(notification.id),
        }
        if self.secret:
            signature = hmac.new(self.secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
            headers["X-NetCI-Signature"] = f"sha256={signature}"

        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            response = await client.post(notification.recipient, content=body, headers=headers)
            response.raise_for_status()


class NotificationOutboxWorker:
    """Background outbox processor implementing exponential backoff and dead-letter queue."""

    def __init__(
        self,
        database: Any,
        dispatcher: NotificationDispatcher | None = None,
        poll_interval_seconds: float = 2.0,
    ) -> None:
        self.database = database
        self.dispatcher = dispatcher or HttpWebhookNotificationDispatcher()
        self.poll_interval_seconds = poll_interval_seconds
        self._running = False
        self._task: asyncio.Task[None] | None = None

    async def run_once(self, now: datetime | None = None) -> int:
        current_time = now or datetime.now(timezone.utc)
        with self.database.transaction() as session:
            pending = session.pending_notifications(limit=50, now=current_time)

        if not pending:
            return 0

        processed = 0
        for notification in pending:
            try:
                await self.dispatcher.dispatch(notification)
                # Success
                with self.database.transaction() as session:
                    session.update_notification_status(
                        notification.id,
                        status=NotificationStatus.DELIVERED,
                        attempt=notification.attempt + 1,
                        next_attempt_at=current_time,
                        last_error=None,
                        delivered_at=current_time,
                    )
                metrics.counter_inc(
                    "netci_notifications_total",
                    {"event_type": notification.event_type, "status": "delivered"},
                )
            except Exception as exc:
                new_attempt = notification.attempt + 1
                error_msg = f"{type(exc).__name__}: {str(exc)}"
                logger.warning(
                    "Notification delivery failed (id=%s, attempt=%d/%d): %s",
                    notification.id,
                    new_attempt,
                    notification.max_attempts,
                    error_msg,
                )

                if new_attempt >= notification.max_attempts:
                    status = NotificationStatus.DEAD_LETTER
                    next_attempt = current_time
                    metrics.counter_inc(
                        "netci_notifications_total",
                        {"event_type": notification.event_type, "status": "dead_letter"},
                    )
                else:
                    status = NotificationStatus.FAILED
                    backoff = min(300, 2 ** new_attempt)
                    next_attempt = current_time + timedelta(seconds=backoff)
                    metrics.counter_inc(
                        "netci_notifications_total",
                        {"event_type": notification.event_type, "status": "failed"},
                    )

                with self.database.transaction() as session:
                    session.update_notification_status(
                        notification.id,
                        status=status,
                        attempt=new_attempt,
                        next_attempt_at=next_attempt,
                        last_error=error_msg,
                    )
            processed += 1

        return processed

    async def _loop(self) -> None:
        while self._running:
            try:
                await self.run_once()
            except Exception as exc:
                logger.error("Outbox worker loop error: %s", exc)
            await asyncio.sleep(self.poll_interval_seconds)

    def start(self) -> None:
        if not self._running:
            self._running = True
            self._task = asyncio.create_task(self._loop())

    def stop(self) -> None:
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
