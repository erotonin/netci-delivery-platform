#!/usr/bin/env python3
"""Apply versioned SQL migrations and keep backend/schema.sql in sync.

Every file in `backend/migrations` is applied once, in filename order, inside its own
transaction, and recorded in `schema_migrations` with the checksum it was applied
with.  Editing a migration that has already run is refused rather than silently
skipped, because a database that no longer matches its recorded history cannot be
rebuilt from Git.

    python scripts/migrate.py --status
    python scripts/migrate.py                 # apply pending migrations
    python scripts/migrate.py --emit-schema   # regenerate backend/schema.sql
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS_DIR = PROJECT_ROOT / "backend" / "migrations"
SCHEMA_FILE = PROJECT_ROOT / "backend" / "schema.sql"

BOOTSTRAP = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version VARCHAR(255) PRIMARY KEY,
    checksum CHAR(64) NOT NULL,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


def migration_files() -> list[Path]:
    files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    if not files:
        raise SystemExit(f"no migrations found in {MIGRATIONS_DIR}")
    return files


def checksum(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def database_url() -> str:
    path = os.getenv("DATABASE_URL_FILE", "").strip()
    if path:
        return Path(path).read_text(encoding="utf-8").strip()
    return os.getenv("DATABASE_URL", "").strip()


def emit_schema() -> int:
    parts = [
        f"-- >>> migration: {path.name}\n{path.read_text(encoding='utf-8').rstrip()}\n"
        for path in migration_files()
    ]
    SCHEMA_FILE.write_text(
        "-- GENERATED FILE - do not edit.\n"
        "-- Concatenation of backend/migrations/*.sql, produced by `python scripts/migrate.py --emit-schema`.\n"
        "-- Used by the compose initdb mount; `make migrate` applies the same files to an existing database.\n\n"
        + "\n".join(parts),
        encoding="utf-8",
    )
    print(f"schema.sql regenerated from {len(parts)} migrations")
    return 0


def check_schema_is_current() -> int:
    """Fail if schema.sql drifted from the migrations that generate it."""

    expected = "\n".join(
        f"-- >>> migration: {path.name}\n{path.read_text(encoding='utf-8').rstrip()}\n"
        for path in migration_files()
    )
    actual = SCHEMA_FILE.read_text(encoding="utf-8")
    if expected not in actual:
        print("backend/schema.sql is stale: run `python scripts/migrate.py --emit-schema`", file=sys.stderr)
        return 1
    print(f"schema.sql matches {len(migration_files())} migrations")
    return 0


def connect(url: str):
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError:  # pragma: no cover - explicit, actionable failure
        raise SystemExit("psycopg is required: pip install -r backend/requirements.txt")
    return psycopg.connect(url, row_factory=dict_row)


def applied_versions(connection) -> dict[str, str]:
    with connection.cursor() as cursor:
        cursor.execute(BOOTSTRAP)
        cursor.execute("SELECT version, checksum FROM schema_migrations")
        return {row["version"]: row["checksum"] for row in cursor.fetchall()}


#: One key for every migration runner against a database. Arbitrary, but fixed: it is
#: what makes two runners wait for each other rather than race.
MIGRATION_LOCK_KEY = 7_265_322_000_001


def run(url: str, *, dry_run: bool) -> int:
    files = migration_files()
    with connect(url) as connection:
        connection.autocommit = False
        # Serialise runners before reading what is applied. Two of them -- a Kubernetes
        # Job retried beside its predecessor, two operators, a rollout that starts one per
        # replica -- used to read the same empty `schema_migrations`, both treat every file
        # as pending, and the second then died mid-way on a relation or a primary key the
        # first had just created. With the lock the second waits, reads the list after the
        # first has committed, and has nothing left to do. Session-scoped: released when
        # this connection closes, including when the process dies.
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_lock(%s)", (MIGRATION_LOCK_KEY,))
        connection.commit()
        applied = applied_versions(connection)
        connection.commit()
        pending = []
        for path in files:
            digest = checksum(path)
            recorded = applied.get(path.name)
            if recorded is None:
                pending.append((path, digest))
            elif recorded != digest:
                print(
                    f"FAIL {path.name}: already applied with a different checksum. "
                    "Add a new migration instead of editing an applied one.",
                    file=sys.stderr,
                )
                return 1
        if not pending:
            print(f"up to date: {len(files)} migrations already applied")
            return 0
        if dry_run:
            for path, _ in pending:
                print(f"PENDING {path.name}")
            return 0
        for path, digest in pending:
            with connection.cursor() as cursor:
                cursor.execute(path.read_text(encoding="utf-8"))
                cursor.execute(
                    "INSERT INTO schema_migrations (version, checksum) VALUES (%s, %s)",
                    (path.name, digest),
                )
            connection.commit()
            print(f"APPLIED {path.name}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--emit-schema", action="store_true", help="regenerate backend/schema.sql and exit")
    parser.add_argument("--check-schema", action="store_true", help="verify schema.sql matches the migrations")
    parser.add_argument("--status", action="store_true", help="list pending migrations without applying them")
    arguments = parser.parse_args()

    if arguments.emit_schema:
        return emit_schema()
    if arguments.check_schema:
        return check_schema_is_current()

    url = database_url()
    if not url:
        print("DATABASE_URL (or DATABASE_URL_FILE) is required", file=sys.stderr)
        return 2
    return run(url, dry_run=arguments.status)


if __name__ == "__main__":
    raise SystemExit(main())
