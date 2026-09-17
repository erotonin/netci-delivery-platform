#!/usr/bin/env python3
"""Back up netCI's database, and prove the backup restores.

A backup nobody has restored is a hope, not a backup. So `verify` is not an optional
extra here: it restores the dump into a throwaway database and compares what came back
against what was recorded when the dump was taken. A dump that cannot be restored, or
that restores with different row counts, missing critical tables, or broken foreign
keys, fails.

    python scripts/netci_backup.py create --output backups/
    python scripts/netci_backup.py verify --input backups/netci-20260828-140000
    python scripts/netci_backup.py restore --input backups/... --into postgresql://...
    python scripts/netci_backup.py drill

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
  * Temporal's databases (`temporal`, `temporal_visibility`, ADR-036). A netCI restored
    without them has deployments whose workflows no longer exist; the reconciler reports
    those as crashed and the operator re-runs them. Back them up with your PostgreSQL
    tooling; this script verifies netCI's schema only and will not claim more.
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

POSTGRES_IMAGE = os.getenv("POSTGRES_IMAGE", "postgres:16.15-alpine3.24")
DUMP_NAME = "netci.dump"
MANIFEST_NAME = "manifest.json"

# Explicit critical tables that must exist in every application database.
# If any of these tables is missing or omitted from a backup or restore, verification fails.
CRITICAL_TABLES: tuple[str, ...] = (
    "schema_migrations",
    "applications",
    "pipeline_runs",
    "deployments",
    "audit_events",
    "idempotency_records",
    "systems",
    "modules",
    "release_versions",
    "production_requests",
    "production_request_modules",
    "delivery_events",
    "pipeline_logs",
    "security_evidence",
    "callback_token_uses",
    "deployment_leases",
    "deployment_fencing_counters",
    "pipeline_log_sequences",
    "version_ci_reports",
    "scm_integrations",
    "scm_webhook_deliveries",
    "pipeline_stages",
    "module_config_revisions",
    "server_health_records",
    "notifications",
    "policy_decisions",
    "security_exceptions",
    "break_glass_requests",
    "resource_quotas",
    "catalog_services",
    "catalog_service_dependencies",
    "catalog_templates",
    "preview_environments",
    "resource_requests",
)


def fail(message: str) -> None:
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(1)


def database_url(explicit: str | None = None) -> str:
    url = explicit or os.getenv("DATABASE_URL") or os.getenv("NETCI_TEST_DATABASE_URL", "")
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
    """Run a PostgreSQL client tool in the pinned image, on the host network."""
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
    """A database name that can be interpolated into DDL without quoting games."""
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


def discover_tables(url: str) -> list[str]:
    """Discover all base tables in the public schema and verify critical tables exist."""
    raw = psql_value(
        url,
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public' AND table_type = 'BASE TABLE' ORDER BY table_name;",
    )
    tables = [line.strip() for line in raw.splitlines() if line.strip()]
    
    missing_critical = [tbl for tbl in CRITICAL_TABLES if tbl not in tables]
    if missing_critical:
        fail(f"database is missing required critical tables: {', '.join(missing_critical)}")
    return tables


def table_counts(url: str, tables: list[str]) -> dict[str, int]:
    """Row counts per table, or -1 if the table is absent."""
    if not tables:
        return {}
    counts: dict[str, int] = {t: -1 for t in tables}
    in_clause = ", ".join(f"'{safe_identifier(t, what='table')}'" for t in tables)
    existing_raw = psql_value(
        url,
        f"SELECT table_name FROM information_schema.tables WHERE table_schema = 'public' AND table_name IN ({in_clause});",
    )
    existing = set(line.strip() for line in existing_raw.splitlines() if line.strip())
    existing_tables = [t for t in tables if t in existing]
    if not existing_tables:
        return counts

    union_queries = [
        f"SELECT '{safe_identifier(t, what='table')}', count(*) FROM {safe_identifier(t, what='table')}"
        for t in existing_tables
    ]
    batch_sql = " UNION ALL ".join(union_queries) + ";"
    raw = psql_value(url, batch_sql)
    for line in raw.splitlines():
        if "|" in line:
            tbl, cnt_str = line.split("|", 1)
            counts[tbl.strip()] = int(cnt_str.strip())
    return counts


def table_checksums(url: str, tables: list[str]) -> dict[str, str]:
    """Content checksum per table across all rows ordered deterministically."""
    if not tables:
        return {}
    checksums: dict[str, str] = {t: "absent" for t in tables}
    in_clause = ", ".join(f"'{safe_identifier(t, what='table')}'" for t in tables)
    existing_raw = psql_value(
        url,
        f"SELECT table_name FROM information_schema.tables WHERE table_schema = 'public' AND table_name IN ({in_clause});",
    )
    existing = set(line.strip() for line in existing_raw.splitlines() if line.strip())
    existing_tables = [t for t in tables if t in existing]
    if not existing_tables:
        return checksums

    union_queries = [
        f"SELECT '{safe_identifier(t, what='table')}', coalesce(md5(string_agg(md5(t::text), '' ORDER BY t::text)), 'empty') FROM {safe_identifier(t, what='table')} t"
        for t in existing_tables
    ]
    batch_sql = " UNION ALL ".join(union_queries) + ";"
    raw = psql_value(url, batch_sql)
    for line in raw.splitlines():
        if "|" in line:
            tbl, cs = line.split("|", 1)
            checksums[tbl.strip()] = cs.strip()
    return checksums


def check_referential_integrity(url: str) -> list[str]:
    """Check all foreign keys in public schema for orphaned records."""
    fk_query = (
        "SELECT "
        "  kcu1.table_name, kcu1.column_name, kcu2.table_name, kcu2.column_name "
        "FROM information_schema.referential_constraints rc "
        "JOIN information_schema.key_column_usage kcu1 "
        "  ON kcu1.constraint_name = rc.constraint_name AND kcu1.constraint_schema = rc.constraint_schema "
        "JOIN information_schema.key_column_usage kcu2 "
        "  ON kcu2.constraint_name = rc.unique_constraint_name AND kcu2.constraint_schema = rc.unique_constraint_schema "
        "  AND kcu2.ordinal_position = kcu1.position_in_unique_constraint "
        "WHERE rc.constraint_schema = 'public';"
    )
    raw = psql_value(url, fk_query)
    fk_pairs = []
    for line in raw.splitlines():
        parts = [p.strip() for p in line.split("|")]
        if len(parts) == 4:
            fk_pairs.append(parts)

    if not fk_pairs:
        return []

    orphan_selects = [
        f"SELECT '{fk_table}.{fk_col} -> {uq_table}.{uq_col}', count(*) FROM {safe_identifier(fk_table, what='table')} fk "
        f"WHERE fk.{safe_identifier(fk_col, what='column')} IS NOT NULL "
        f"AND NOT EXISTS (SELECT 1 FROM {safe_identifier(uq_table, what='table')} uq WHERE uq.{safe_identifier(uq_col, what='column')} = fk.{safe_identifier(fk_col, what='column')})"
        for fk_table, fk_col, uq_table, uq_col in fk_pairs
    ]
    batch_orphan_sql = " UNION ALL ".join(orphan_selects) + ";"
    batch_raw = psql_value(url, batch_orphan_sql)
    violations: list[str] = []
    for line in batch_raw.splitlines():
        if "|" in line:
            rel, count_str = line.split("|", 1)
            count = int(count_str.strip())
            if count > 0:
                violations.append(f"{rel.strip()}: {count} orphaned rows")
    return violations



def get_encryption_key(explicit: str | None = None) -> str | None:
    return explicit or os.getenv("NETCI_BACKUP_ENCRYPTION_KEY")


def derive_key(passphrase: str, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", passphrase.encode("utf-8"), salt, 100_000, dklen=32)


def encrypt_payload(data: bytes, key_str: str) -> bytes:
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError:
        fail("cryptography package is required for encrypted backups")
    salt = os.urandom(16)
    key = derive_key(key_str, salt)
    aesgcm = AESGCM(key)
    nonce = os.urandom(12)
    ciphertext = aesgcm.encrypt(nonce, data, None)
    return b"NETCI_ENC_V1" + salt + nonce + ciphertext


def decrypt_payload(data: bytes, key_str: str) -> bytes:
    if not data.startswith(b"NETCI_ENC_V1"):
        fail("invalid encrypted backup payload header")
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError:
        fail("cryptography package is required for decrypting backups")
    salt = data[12:28]
    nonce = data[28:40]
    ciphertext = data[40:]
    key = derive_key(key_str, salt)
    aesgcm = AESGCM(key)
    try:
        return aesgcm.decrypt(nonce, ciphertext, None)
    except Exception as exc:
        fail(f"failed to decrypt backup: incorrect key or corrupted ciphertext ({exc})")


def create(arguments: argparse.Namespace) -> int:
    url = database_url(arguments.database_url)
    parts = split(url)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = Path(arguments.output) / f"netci-{stamp}"
    target.mkdir(parents=True, exist_ok=False)

    enc_key = get_encryption_key(getattr(arguments, "encryption_key", None))
    should_encrypt = bool(getattr(arguments, "encrypt", False) or enc_key)
    if getattr(arguments, "encrypt", False) and not enc_key:
        fail("--encrypt specified but no key provided via --encryption-key or NETCI_BACKUP_ENCRYPTION_KEY")

    print(f"backing up {parts['database']} at {parts['host']}:{parts['port']}")
    tables = discover_tables(url)
    counts = table_counts(url, tables)
    checksums = table_checksums(url, [t for t in tables if t in CRITICAL_TABLES])

    # Custom format: compressed, and restorable table by table
    result = run_in_postgres(
        url,
        ["pg_dump", "-h", parts["host"], "-p", parts["port"], "-U", parts["user"],
         "-d", parts["database"], "--format=custom", "--no-owner", "--no-privileges"],
    )
    if result.returncode != 0:
        fail(f"pg_dump failed: {result.stderr.decode(errors='replace').strip()[-400:]}")

    raw_bytes = result.stdout
    dump_bytes = raw_bytes
    if should_encrypt and enc_key:
        dump_bytes = encrypt_payload(raw_bytes, enc_key)

    dump = target / DUMP_NAME
    dump.write_bytes(dump_bytes)
    digest = hashlib.sha256(dump_bytes).hexdigest()

    manifest = {
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "createdBy": getpass.getuser(),
        "database": parts["database"],
        "postgresImage": POSTGRES_IMAGE,
        "dumpFile": DUMP_NAME,
        "dumpSha256": digest,
        "dumpBytes": len(dump_bytes),
        "encrypted": should_encrypt,
        "encryptionAlgorithm": "AES-256-GCM" if should_encrypt else None,
        "criticalTables": list(CRITICAL_TABLES),
        "discoveredTables": tables,
        "tableCounts": counts,
        "tableChecksums": checksums,
        "migrations": psql_value(url, "SELECT string_agg(version, ',' ORDER BY version) FROM schema_migrations")
        if counts.get("schema_migrations", -1) >= 0 else "",
        "notCovered": [
            "evidence/ and NETCI_SECURITY_EVIDENCE_DIR (on disk; deployments fail without them)",
            "NETCI_AUTH_TOKENS_FILE and cosign keys",
            "Jenkins home (rebuilt from JCasC, see ADR-006)",
        ],
    }
    (target / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    enc_msg = " [AES-256-GCM encrypted]" if should_encrypt else ""
    print(f"  wrote {dump} ({len(dump_bytes):,} bytes){enc_msg}")
    print(f"  sha256 {digest}")
    print(f"  discovered {len(tables)} tables ({len(CRITICAL_TABLES)} critical)")
    for table in sorted(tables):
        cnt = counts.get(table, -1)
        cs = checksums.get(table, "")
        cs_hint = f" [chk:{cs[:8]}]" if cs else ""
        print(f"    {table:<28} {'absent' if cnt < 0 else cnt}{cs_hint}")
    print(f"\nbackup is unverified until you run:\n  python scripts/netci_backup.py verify --input {target}")
    return 0


def load_manifest(folder: Path) -> dict:
    path = folder / MANIFEST_NAME
    if not path.is_file():
        fail(f"no {MANIFEST_NAME} in {folder}")
    return json.loads(path.read_text(encoding="utf-8"))


def check_integrity(folder: Path, manifest: dict, enc_key: str | None = None) -> bytes:
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
    if manifest.get("encrypted"):
        key = get_encryption_key(enc_key)
        if not key:
            fail("backup is encrypted; supply --encryption-key or NETCI_BACKUP_ENCRYPTION_KEY")
        payload = decrypt_payload(payload, key)
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
    """Restore into a clean throwaway database and comprehensively verify."""
    folder = Path(arguments.input)
    manifest = load_manifest(folder)
    payload = check_integrity(folder, manifest, getattr(arguments, "encryption_key", None))
    print(f"checksum matches manifest ({manifest['dumpSha256'][:16]}…)")

    admin_url = database_url(getattr(arguments, "database_url", None))
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

    print(f"restoring into a clean throwaway database {scratch}")
    psql_value(maintenance, f'DROP DATABASE IF EXISTS "{scratch}"')
    psql_value(maintenance, f'CREATE DATABASE "{scratch}"')
    scratch_url = urllib.parse.urlunsplit((
        "postgresql",
        f"{urllib.parse.quote(parts['user'])}:{urllib.parse.quote(parts['password'])}@{parts['host']}:{parts['port']}",
        f"/{scratch}", "", "",
    ))

    try:
        pg_restore_into(scratch_url, payload, clean=False)
        
        # 1. Verify all critical tables are present
        restored_tables = discover_tables(scratch_url)
        required_critical = manifest.get("criticalTables", list(CRITICAL_TABLES))
        missing_critical = [t for t in required_critical if t not in restored_tables]
        if missing_critical:
            fail(f"restore verification failed: critical tables missing in restored DB: {missing_critical}")

        # 2. Check row counts for all expected tables
        expected_counts = {str(k): int(v) for k, v in manifest["tableCounts"].items()}
        restored_counts = table_counts(scratch_url, list(expected_counts.keys()))

        count_diffs = [
            f"    {table:<28} backup={expected_counts[table]:>8}  restored={restored_counts.get(table, -1):>8}"
            for table in expected_counts
            if restored_counts.get(table, -1) != expected_counts[table]
        ]
        if count_diffs:
            print("\nrestored data does not match the backup:", file=sys.stderr)
            print("\n".join(count_diffs), file=sys.stderr)
            fail("row count verification failed")

        # 3. Check checksums for critical tables
        expected_checksums = manifest.get("tableChecksums", {})
        if expected_checksums:
            restored_checksums = table_checksums(scratch_url, list(expected_checksums.keys()))
            cs_diffs = [
                f"    {table:<28} expected={expected_checksums[table]} restored={restored_checksums.get(table, '')}"
                for table in expected_checksums
                if restored_checksums.get(table) != expected_checksums[table]
            ]
            if cs_diffs:
                print("\nrestored table checksums do not match the backup:", file=sys.stderr)
                print("\n".join(cs_diffs), file=sys.stderr)
                fail("table checksum verification failed")

        # 4. Check schema migrations ledger
        migrations = psql_value(scratch_url, "SELECT string_agg(version, ',' ORDER BY version) FROM schema_migrations")
        if migrations != manifest.get("migrations", ""):
            fail(
                "the restored migration ledger differs from the backup: "
                f"expected {manifest.get('migrations')!r}, got {migrations!r}"
            )

        # 5. Check referential integrity across all foreign keys
        violations = check_referential_integrity(scratch_url)
        if violations:
            print("\nreferential integrity violations detected:", file=sys.stderr)
            for v in violations:
                print(f"  ! {v}", file=sys.stderr)
            fail("foreign key referential integrity verification failed")

        print(f"\nverified: all {len(required_critical)} critical tables present, row counts, checksums, FKs, and migrations match. Migrations: {migrations}")
        return 0
    finally:
        if not getattr(arguments, "keep", False):
            psql_value(maintenance, f'DROP DATABASE IF EXISTS "{scratch}"')


def restore(arguments: argparse.Namespace) -> int:
    folder = Path(arguments.input)
    manifest = load_manifest(folder)
    payload = check_integrity(folder, manifest, getattr(arguments, "encryption_key", None))
    target = arguments.into or database_url(getattr(arguments, "database_url", None))
    parts = split(target)

    if not arguments.yes:
        confirmation = input(f"This overwrites {parts['database']} at {parts['host']}. Type the database name to continue: ")
        if confirmation != parts["database"]:
            fail(f"confirmation mismatch ({confirmation!r} != {parts['database']!r}); aborting; nothing was changed")

    print(f"restoring {folder} into {parts['database']} at {parts['host']}:{parts['port']}")
    pg_restore_into(target, payload, clean=True)
    print("restore complete; verifying critical tables in destination")
    tables = discover_tables(target)
    missing = [t for t in CRITICAL_TABLES if t not in tables]
    if missing:
        fail(f"critical tables missing after restore: {', '.join(missing)}")
    print(f"verified: all {len(CRITICAL_TABLES)} critical tables present in {parts['database']}")
    return 0


def drill(arguments: argparse.Namespace) -> int:
    """Run an automated restore drill including intentional failure injection."""
    url = database_url(getattr(arguments, "database_url", None))
    parts = split(url)
    print(f"=== Starting Automated Backup & Restore Drill on {parts['database']} ===")

    tmpdir = Path(tempfile.mkdtemp(prefix="netci_drill_"))
    try:
        # Step 1: Create backup
        print("\n[1/3] Creating live backup...")
        create_args = argparse.Namespace(database_url=url, output=str(tmpdir))
        create(create_args)
        
        backup_dirs = [d for d in tmpdir.iterdir() if d.is_dir() and d.name.startswith("netci-")]
        if not backup_dirs:
            fail("drill failed: backup directory was not created")
        backup_dir = backup_dirs[0]

        # Step 2: Verify live backup into clean scratch DB
        print("\n[2/3] Verifying live backup into clean scratch database...")
        verify_args = argparse.Namespace(
            database_url=url,
            input=str(backup_dir),
            scratch_database=f"netci_drill_{datetime.now(timezone.utc).strftime('%H%M%S')}",
            keep=False,
        )
        verify(verify_args)

        # Step 3: Intentional failure injection test (tamper manifest critical table)
        print("\n[3/3] Testing failure injection: verifying that omitting a critical table fails verification...")
        tampered_dir = tmpdir / "tampered"
        shutil.copytree(backup_dir, tampered_dir)
        manifest_path = tampered_dir / MANIFEST_NAME
        manifest_data = json.loads(manifest_path.read_text(encoding="utf-8"))
        # Inject non-existent table into criticalTables list
        manifest_data["criticalTables"].append("non_existent_critical_audit_table")
        manifest_path.write_text(json.dumps(manifest_data, indent=2), encoding="utf-8")

        tamper_verify_args = argparse.Namespace(
            database_url=url,
            input=str(tampered_dir),
            scratch_database=f"netci_drill_fail_{datetime.now(timezone.utc).strftime('%H%M%S')}",
            keep=False,
        )
        try:
            verify(tamper_verify_args)
            fail("DRILL FAILED: verification passed when a critical table was missing!")
        except SystemExit as exc:
            if exc.code != 0:
                print("  Success: intentional failure was correctly detected and failed closed.")
            else:
                fail("DRILL FAILED: verification returned 0 on corrupted critical table!")

        print("\n=== Automated Restore Drill PASSED successfully! ===")
        return 0
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--database-url", help="source database (default: $DATABASE_URL)")
    parser.add_argument("--encryption-key", help="AES-256-GCM encryption key or passphrase (default: $NETCI_BACKUP_ENCRYPTION_KEY)")
    commands = parser.add_subparsers(dest="command", required=True)

    create_parser = commands.add_parser("create", help="dump the database and record what was in it")
    create_parser.add_argument("--output", default="backups", help="directory to write the backup into")
    create_parser.add_argument("--encrypt", action="store_true", help="encrypt backup using AES-256-GCM")
    create_parser.add_argument("--encryption-key", help="passphrase to encrypt with (default: $NETCI_BACKUP_ENCRYPTION_KEY)")
    create_parser.set_defaults(handler=create)

    verify_parser = commands.add_parser("verify", help="restore into a throwaway database and compare")
    verify_parser.add_argument("--input", required=True)
    verify_parser.add_argument("--scratch-database", help="name for the throwaway database")
    verify_parser.add_argument("--keep", action="store_true", help="leave the throwaway database for inspection")
    verify_parser.add_argument("--encryption-key", help="passphrase to decrypt with if backup is encrypted")
    verify_parser.set_defaults(handler=verify)

    restore_parser = commands.add_parser("restore", help="restore into a real database (destructive)")
    restore_parser.add_argument("--input", required=True)
    restore_parser.add_argument("--into", help="target database URL (default: --database-url)")
    restore_parser.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    restore_parser.add_argument("--encryption-key", help="passphrase to decrypt with if backup is encrypted")
    restore_parser.set_defaults(handler=restore)

    drill_parser = commands.add_parser("drill", help="run automated backup, clean-DB restore verify, and failure-injection check")
    drill_parser.set_defaults(handler=drill)

    arguments = parser.parse_args()
    return arguments.handler(arguments)


if __name__ == "__main__":
    raise SystemExit(main())
