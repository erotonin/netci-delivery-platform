"""Data retention policy manager for expired callback tokens, completed notifications, and aged events."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)


class RetentionManager:
    """Purges expired or aged transient platform records safely."""

    def __init__(
        self,
        database: Any,
        token_retention_grace_seconds: int = 86400,
        notification_retention_days: int = 30,
        event_retention_days: int = 90,
    ) -> None:
        self.database = database
        self.token_retention_grace_seconds = token_retention_grace_seconds
        self.notification_retention_days = notification_retention_days
        self.event_retention_days = event_retention_days

    def purge_all(self, now: datetime | None = None) -> dict[str, int]:
        current_time = now or datetime.now(timezone.utc)
        token_cutoff = current_time - timedelta(seconds=self.token_retention_grace_seconds)
        notification_cutoff = current_time - timedelta(days=self.notification_retention_days)
        event_cutoff = current_time - timedelta(days=self.event_retention_days)

        with self.database.transaction() as session:
            tokens_purged = session.purge_expired_callback_tokens(token_cutoff)
            notifs_purged = session.purge_completed_notifications(notification_cutoff)
            events_purged = session.purge_old_delivery_events(event_cutoff)

        logger.info(
            "Retention purge completed: %d expired callback tokens, %d delivered notifications, %d delivery events",
            tokens_purged,
            notifs_purged,
            events_purged,
        )
        return {
            "expired_callback_tokens": tokens_purged,
            "completed_notifications": notifs_purged,
            "aged_delivery_events": events_purged,
        }
