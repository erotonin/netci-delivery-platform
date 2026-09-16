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
        pipeline_log_retention_days: int = 90,
    ) -> None:
        self.database = database
        self.token_retention_grace_seconds = token_retention_grace_seconds
        self.notification_retention_days = notification_retention_days
        self.event_retention_days = event_retention_days
        self.pipeline_log_retention_days = pipeline_log_retention_days

    @classmethod
    def from_environment(cls, database: Any) -> "RetentionManager":
        """Windows from configuration. Audit events are not on this list on purpose: an
        audit trail that a retention job thins is not an audit trail; archiving it is a
        decision for whoever answers to the auditor, not a default."""

        import os

        def days(name: str, default: int) -> int:
            try:
                return max(1, int(os.getenv(name, str(default))))
            except ValueError:
                return default

        return cls(
            database,
            notification_retention_days=days("NETCI_RETENTION_NOTIFICATION_DAYS", 30),
            event_retention_days=days("NETCI_RETENTION_EVENT_DAYS", 90),
            pipeline_log_retention_days=days("NETCI_RETENTION_PIPELINE_LOG_DAYS", 90),
        )

    def purge_all(self, now: datetime | None = None) -> dict[str, int]:
        current_time = now or datetime.now(timezone.utc)
        token_cutoff = current_time - timedelta(seconds=self.token_retention_grace_seconds)
        notification_cutoff = current_time - timedelta(days=self.notification_retention_days)
        event_cutoff = current_time - timedelta(days=self.event_retention_days)
        log_cutoff = current_time - timedelta(days=self.pipeline_log_retention_days)

        with self.database.transaction() as session:
            tokens_purged = session.purge_expired_callback_tokens(token_cutoff)
            notifs_purged = session.purge_completed_notifications(notification_cutoff)
            events_purged = session.purge_old_delivery_events(event_cutoff)
            logs_purged = session.purge_old_pipeline_logs(log_cutoff)

        logger.info(
            "Retention purge completed: %d expired callback tokens, %d delivered notifications, %d delivery events, %d pipeline log lines",
            tokens_purged,
            notifs_purged,
            events_purged,
            logs_purged,
        )
        return {
            "expired_callback_tokens": tokens_purged,
            "completed_notifications": notifs_purged,
            "aged_delivery_events": events_purged,
            "aged_pipeline_log_lines": logs_purged,
        }
