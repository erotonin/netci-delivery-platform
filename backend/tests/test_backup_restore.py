"""Backup and restore, against a real PostgreSQL.

A backup nobody has restored is a hope. These tests are mostly about the ways `verify`
must *fail*: a truncated dump and a restore that silently loses rows are the two ways a
backup disappoints you at the worst possible moment, and both look fine until someone
checks.

Skipped unless NETCI_TEST_DATABASE_URL points at a migrated database; the `database` job
in CI supplies one, which is the only place these run on every change.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import urllib.parse
import uuid
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
DATABASE_URL = os.getenv("NETCI_TEST_DATABASE_URL", "")

pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="NETCI_TEST_DATABASE_URL is not set")


@pytest.fixture(scope="module")
def backup_tool():
    """Load the script the way an operator runs it: as a standalone file."""

    spec = importlib.util.spec_from_file_location("netci_backup", ROOT / "scripts" / "netci_backup.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["netci_backup"] = module
    spec.loader.exec_module(module)
    return module


def run(*arguments: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "netci_backup.py"), "--database-url", DATABASE_URL, *arguments],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )


@pytest.fixture
def backup(tmp_path):
    result = run("create", "--output", str(tmp_path))
    assert result.returncode == 0, result.stderr
    folder = next(Path(tmp_path).glob("netci-*"))
    return folder


def manifest_of(folder: Path) -> dict:
    return json.loads((folder / "manifest.json").read_text(encoding="utf-8"))


# ------------------------------------------------------------------ what it records


def test_a_backup_records_what_was_in_it_not_just_the_bytes(backup):
    """Without recorded counts there is nothing to compare a restore against."""

    manifest = manifest_of(backup)

    assert (backup / manifest["dumpFile"]).stat().st_size == manifest["dumpBytes"] > 0
    assert len(manifest["dumpSha256"]) == 64
    assert manifest["tableCounts"]["schema_migrations"] > 0
    # The migration ledger, so a restore can be checked against the schema it expects.
    assert "0001_baseline.sql" in manifest["migrations"]
    # And an explicit statement of the gap, because a restore plan with a silent one is
    # worse than no plan.
    assert any("evidence" in item for item in manifest["notCovered"])


# ------------------------------------------------------------------- the happy path


def test_verify_restores_the_dump_and_matches_every_table(backup):
    result = run("verify", "--input", str(backup), "--scratch-database", f"verify_{uuid.uuid4().hex[:8]}")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "verified" in result.stdout
    assert "0001_baseline.sql" in result.stdout


def test_the_throwaway_database_is_cleaned_up(backup, backup_tool):
    scratch = f"verify_{uuid.uuid4().hex[:8]}"
    assert run("verify", "--input", str(backup), "--scratch-database", scratch).returncode == 0

    parts = backup_tool.split(DATABASE_URL)
    maintenance = urllib.parse.urlunsplit((
        "postgresql",
        f"{parts['user']}:{urllib.parse.quote(parts['password'])}@{parts['host']}:{parts['port']}",
        "/postgres", "", "",
    ))
    remaining = backup_tool.psql_value(
        maintenance, f"SELECT count(*) FROM pg_database WHERE datname = '{scratch}'"
    )
    assert remaining == "0", "a verify that leaves databases behind fills the disk of whoever automates it"


# ------------------------------------------------------- the ways it has to fail


def test_a_truncated_dump_is_caught_before_anything_is_restored(backup):
    """Silent truncation is the classic way a backup disappoints you."""

    dump = backup / manifest_of(backup)["dumpFile"]
    dump.write_bytes(dump.read_bytes()[: len(dump.read_bytes()) // 2])

    result = run("verify", "--input", str(backup))
    assert result.returncode != 0
    assert "checksum" in result.stderr
    assert "truncated" in result.stderr


def test_a_restore_that_loses_rows_fails_rather_than_reporting_success(backup):
    """The check that makes this a verification instead of a `pg_restore` exit code."""

    path = backup / "manifest.json"
    manifest = manifest_of(backup)
    manifest["tableCounts"]["applications"] = manifest["tableCounts"]["applications"] + 500
    path.write_text(json.dumps(manifest), encoding="utf-8")

    result = run("verify", "--input", str(backup), "--scratch-database", f"verify_{uuid.uuid4().hex[:8]}")
    assert result.returncode != 0
    assert "does not match the backup" in result.stderr
    assert "applications" in result.stderr


def test_a_missing_manifest_is_refused(tmp_path):
    empty = tmp_path / "not-a-backup"
    empty.mkdir()
    result = run("verify", "--input", str(empty))
    assert result.returncode != 0
    assert "manifest.json" in result.stderr


def test_restore_will_not_overwrite_without_confirmation(backup):
    """A restore points at a live database. Requiring the name back costs nothing in a
    drill and is the cheapest protection against choosing the wrong one."""

    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "netci_backup.py"), "restore",
         "--input", str(backup), "--into", DATABASE_URL],
        input="definitely-not-the-database-name\n",
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    assert result.returncode != 0
    assert "nothing was changed" in result.stderr
