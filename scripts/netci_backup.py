#!/usr/bin/env python3
"""Back up netCI's database, and prove the backup restores.

A backup nobody has restored is a hope, not a backup. So `verify` is not an optional
extra here: it restores the dump into a throwaway database and compares what came back
against what was recorded when the dump was taken. A dump that cannot be restored, or
that restores with different row counts, fails.

    python scripts/netci_backup.py create --output backups/
    python scripts/netci_backup.py verify --input backups/netci-20260828-140000
    python scripts/netci_backup.py restore --input backups/... --into postgresql://...

`pg_dump` runs inside the project's pinned PostgreSQL image rather than from the host.
Two reasons: the host usually has no PostgreSQL client tools, and pg_dump refuses to dump
a server newer than itself -- pinning both to the same image makes that mismatch
impossible rather than a thing to remember.

What this does NOT cover, stated plainly because a restore plan with a silent gap is
worse than none:

  * Evidence files under evidence/ and any NETCI_SECURITY_EVIDENCE_DIR. They live on
    disk, and the supply-chain policy reads them at deploy time -- a database restored
    without them will refuse deployments it previously allowed.
  * The token file, the cosign keys, and anything else in the secret manager.
  * Jenkins home. That is rebuilt from JCasC by design; see ADR-006.
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

POSTGRES_IMAGE = os.getenv("POSTGRES_IMAGE", "postgres:16.15-alpine3.24")
DUMP_NAME = "netci.dump"
MANIFEST_NAME = "manifest.json"

# Every table whose loss would be noticed. `schema_migrations` is included because a
# restore that lands the data but not the migration ledger looks healthy and then fails
# the next `migrate.py --check-schema`.
COUNTED_TABLES = (
    "schema_migrations",
    "applications",
    "pipeline_runs",
    "pipeline_logs",
    "deployments",
    "delivery_events",
    "audit_events",
    "idempotency_records",
)


def fail(message: str) -> None:
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(1)


def database_url(explicit: str | None) -> str:
    url = explicit or os.getenv("DATABASE_URL", "")
    if not url:
        fail("set --database-url or DATABASE_URL")
    return url


def split(url: str) -> dict[str, str]:
    parsed = urllib.parse.urlsplit(url)
    if not parsed.hostname or not parsed.path.lstrip("/"):
        fail(f"DATABASE_URL must include a host and a database name: {url!r}")
    return {
        "host": parsed.hostname,
        "port": str(parsed.port or 5432),
        "user": urllib.parse.unquote(parsed.username or "netci"),
        "password": urllib.parse.unquote(parsed.password or ""),
        "database": parsed.path.lstrip("/"),
    }


def run_in_postgres(url: str, command: list[str], *, stdin: bytes | None = None, mounts: list[str] | None = None) -> subprocess.CompletedProcess:
    """Run a PostgreSQL client tool in the pinned image, on the host network.

    Host networking because the database is reachable at whatever host and port the URL
    says; publishing a port into a fresh container to reach a service already listening
    on the host would only add a way to get it wrong.
    """

    parts = split(url)
    docker = [
        "docker", "run", "--rm", "-i", "--network", "host",
        "-e", f"PGPASSWORD={parts['password']}",
    ]
    for mount in mounts or []:
        docker += ["-v", mount]
    docker += [POSTGRES_IMAGE, *command]
    return subprocess.run(docker, input=stdin, capture_output=True)


def safe_identifier(name: str, *, what: str) -> str:
    """A database name that can be interpolated into DDL without quoting games.

    CREATE and DROP DATABASE cannot take a bind parameter, so the name goes into the
    statement text. Restricting it to the shape PostgreSQL accepts unquoted means there
    is nothing to escape -- rather than escaping carefully and hoping.
    """

    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,62}", name):
        fail(
            f"{what} must be a plain PostgreSQL identifier (letters, digits and "
            f"underscores, not starting with a digit): {name!r}"
        )
    return name


def psql_value(url: str, sql: str) -> str:
    parts = split(url)
    result = run_in_postgres(
        url,
        ["psql", "-h", parts["host"], "-p", parts["port"], "-U", parts["user"],
         "-d", parts["database"], "-tAc", sql],
    )
    if result.returncode != 0:
        fail(f"psql failed: {result.stderr.decode(errors='replace').strip()[-300:]}")
    return result.stdout.decode().strip()


def table_counts(url: str) -> dict[str, int]:
    """Row counts per table, or -1 for a table this deployment does not have.

    -1 rather than 0: "the table is absent" and "the table is empty" are different facts,
    and a verify that treats them alike would pass a restore that lost a whole table.
    """

    counts: dict[str, int] = {}
    for table in COUNTED_TABLES:
        exists = psql_value(url, f"SELECT to_regclass('public.{table}') IS NOT NULL")
        counts[table] = int(psql_value(url, f"SELECT count(*) FROM {table}")) if exists == "t" else -1
    return counts


def create(arguments: argparse.Namespace) -> int:
    url = database_url(arguments.database_url)
    parts = split(url)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = Path(arguments.output) / f"netci-{stamp}"
    target.mkdir(parents=True, exist_ok=False)

    print(f"backing up {parts['database']} at {parts['host']}:{parts['port']}")
    counts = table_counts(url)

    # Custom format: compressed, and restorable table by table when only one thing was
    # lost. A plain SQL dump can only be replayed whole.
    result = run_in_postgres(
        url,
        ["pg_dump", "-h", parts["host"], "-p", parts["port"], "-U", parts["user"],
         "-d", parts["database"], "--format=custom", "--no-owner", "--no-privileges"],
    )
    if result.returncode != 0:
        fail(f"pg_dump failed: {result.stderr.decode(errors='replace').strip()[-400:]}")

    dump = target / DUMP_NAME
    dump.write_bytes(result.stdout)
    digest = hashlib.sha256(result.stdout).hexdigest()

    manifest = {
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "createdBy": getpass.getuser(),
        "database": parts["database"],
        "postgresImage": POSTGRES_IMAGE,
        "dumpFile": DUMP_NAME,
        "dumpSha256": digest,
        "dumpBytes": len(result.stdout),
        "tableCounts": counts,
        # The ledger, so a restore can be checked against the schema it expects rather
        # than only against itself.
        "migrations": psql_value(url, "SELECT string_agg(version, ',' ORDER BY version) FROM schema_migrations")
        if counts.get("schema_migrations", -1) >= 0 else "",
        "notCovered": [
            "evidence/ and NETCI_SECURITY_EVIDENCE_DIR (on disk; deployments fail without them)",
            "NETCI_AUTH_TOKENS_FILE and cosign keys",
            "Jenkins home (rebuilt from JCasC, see ADR-006)",
        ],
    }
    (target / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    print(f"  wrote {dump} ({len(result.stdout):,} bytes)")
    print(f"  sha256 {digest}")
    for table, count in counts.items():
        print(f"    {table:<22} {'absent' if count < 0 else count}")
    print(f"\nbackup is unverified until you run:\n  python scripts/netci_backup.py verify --input {target}")
    return 0


def load_manifest(folder: Path) -> dict:
    path = folder / MANIFEST_NAME
    if not path.is_file():
        fail(f"no {MANIFEST_NAME} in {folder}")
    return json.loads(path.read_text(encoding="utf-8"))


def check_integrity(folder: Path, manifest: dict) -> bytes:
    dump = folder / str(manifest["dumpFile"])
    if not dump.is_file():
        fail(f"missing dump file {dump}")
    payload = dump.read_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    if digest != manifest["dumpSha256"]:
        fail(
            f"dump does not match its manifest checksum: expected {manifest['dumpSha256']}, "
            f"got {digest}. The file has been altered or truncated since it was written."
        )
    return payload


def pg_restore_into(url: str, payload: bytes, *, clean: bool) -> None:
    parts = split(url)
    command = [
        "pg_restore", "-h", parts["host"], "-p", parts["port"], "-U", parts["user"],
        "-d", parts["database"], "--no-owner", "--no-privileges", "--exit-on-error",
    ]
    if clean:
        command.append("--clean")
    result = run_in_postgres(url, command, stdin=payload)
    if result.returncode != 0:
        fail(f"pg_restore failed: {result.stderr.decode(errors='replace').strip()[-600:]}")


def verify(arguments: argparse.Namespace) -> int:
    """Restore into a throwaway database and compare. This is what makes it a backup."""

    folder = Path(arguments.input)
    manifest = load_manifest(folder)
    payload = check_integrity(folder, manifest)
    print(f"checksum matches manifest ({manifest['dumpSha256'][:16]}…)")

    admin_url = database_url(arguments.database_url)
    parts = split(admin_url)
    scratch = safe_identifier(
        arguments.scratch_database or f"netci_verify_{datetime.now(timezone.utc).strftime('%H%M%S')}",
        what="--scratch-database",
    )
    maintenance = urllib.parse.urlunsplit((
        "postgresql",
        f"{urllib.parse.quote(parts['user'])}:{urllib.parse.quote(parts['password'])}@{parts['host']}:{parts['port']}",
        "/postgres", "", "",
    ))

    print(f"restoring into a throwaway database {scratch}")
    psql_value(maintenance, f'DROP DATABASE IF EXISTS "{scratch}"')
    psql_value(maintenance, f'CREATE DATABASE "{scratch}"')
    scratch_url = urllib.parse.urlunsplit((
        "postgresql",
        f"{urllib.parse.quote(parts['user'])}:{urllib.parse.quote(parts['password'])}@{parts['host']}:{parts['port']}",
        f"/{scratch}", "", "",
    ))

    try:
        pg_restore_into(scratch_url, payload, clean=False)
        restored = table_counts(scratch_url)
        expected = {str(k): int(v) for k, v in manifest["tableCounts"].items()}

        differences = [
            f"    {table:<22} backup={expected[table]:>8}  restored={restored.get(table, -1):>8}"
            for table in expected
            if restored.get(table, -1) != expected[table]
        ]
        print("  row counts:")
        for table, count in restored.items():
            marker = " " if restored.get(table, -1) == expected.get(table) else "!"
            print(f"  {marker} {table:<22} {'absent' if count < 0 else count}")

        if differences:
            print("\nrestored data does not match the backup:", file=sys.stderr)
            print("\n".join(differences), file=sys.stderr)
            fail("verification failed")

        migrations = psql_value(scratch_url, "SELECT string_agg(version, ',' ORDER BY version) FROM schema_migrations")
        if migrations != manifest.get("migrations", ""):
            fail(
                "the restored migration ledger differs from the backup: "
                f"expected {manifest.get('migrations')!r}, got {migrations!r}"
            )

        print(f"\nverified: the dump restores and every table matches. Migrations: {migrations}")
        return 0
    finally:
        if not arguments.keep:
            psql_value(maintenance, f'DROP DATABASE IF EXISTS "{scratch}"')


def restore(arguments: argparse.Namespace) -> int:
    folder = Path(arguments.input)
    manifest = load_manifest(folder)
    payload = check_integrity(folder, manifest)
    target = arguments.into or database_url(arguments.database_url)
    parts = split(target)

    # A restore overwrites a live database. Requiring the name back is the cheapest
    # protection against pointing it at the wrong one, and costs nothing in a drill.
    if not arguments.yes:
        confirmation = input(f"This overwrites {parts['database']} at {parts['host']}. Type the database name to continue: ")
        if confirmation.strip() != parts["database"]:
            fail("confirmation did not match; nothing was changed")

    print(f"restoring {manifest['dumpBytes']:,} bytes into {parts['database']}")
    pg_restore_into(target, payload, clean=True)
    counts = table_counts(target)
    for table, count in counts.items():
        print(f"    {table:<22} {'absent' if count < 0 else count}")
    print("\nrestore complete. Remember what the dump does not contain:")
    for item in manifest.get("notCovered", []):
        print(f"  - {item}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--database-url", help="source database (default: $DATABASE_URL)")
    commands = parser.add_subparsers(dest="command", required=True)

    create_parser = commands.add_parser("create", help="dump the database and record what was in it")
    create_parser.add_argument("--output", default="backups", help="directory to write the backup into")
    create_parser.set_defaults(handler=create)

    verify_parser = commands.add_parser("verify", help="restore into a throwaway database and compare")
    verify_parser.add_argument("--input", required=True)
    verify_parser.add_argument("--scratch-database", help="name for the throwaway database")
    verify_parser.add_argument("--keep", action="store_true", help="leave the throwaway database for inspection")
    verify_parser.set_defaults(handler=verify)

    restore_parser = commands.add_parser("restore", help="restore into a real database (destructive)")
    restore_parser.add_argument("--input", required=True)
    restore_parser.add_argument("--into", help="target database URL (default: --database-url)")
    restore_parser.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    restore_parser.set_defaults(handler=restore)

    arguments = parser.parse_args()
    return arguments.handler(arguments)


if __name__ == "__main__":
    raise SystemExit(main())
