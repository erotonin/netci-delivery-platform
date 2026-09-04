#!/usr/bin/env python3
"""CLI utility to execute data retention purges on the canonical database."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.app.persistence import database_url
from backend.app.retention import RetentionManager
from backend.app.store.postgres import PostgresDatabase


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", help="Database URL (default: $DATABASE_URL)")
    parser.add_argument(
        "--token-grace-seconds",
        type=int,
        default=86400,
        help="Grace period after expiry before callback tokens are deleted (default: 86400)",
    )
    parser.add_argument(
        "--notification-days",
        type=int,
        default=30,
        help="Retention period in days for delivered notifications (default: 30)",
    )
    parser.add_argument(
        "--event-days",
        type=int,
        default=90,
        help="Retention period in days for delivery events (default: 90)",
    )
    args = parser.parse_args()

    url = args.database_url or database_url()
    if not url:
        print("error: DATABASE_URL is required", file=sys.stderr)
        return 1

    db = PostgresDatabase(url)
    manager = RetentionManager(
        database=db,
        token_retention_grace_seconds=args.token_grace_seconds,
        notification_retention_days=args.notification_days,
        event_retention_days=args.event_days,
    )

    print("Running retention purge...")
    results = manager.purge_all()
    for category, count in results.items():
        print(f"  {category}: {count} purged")
    print("Retention purge complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
