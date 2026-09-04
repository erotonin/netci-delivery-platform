import json
import os
from uuid import uuid4

import pytest

from app.persistence import VersionConflict
from app.store import SystemRow, ModuleRow, VersionRow
from app.store.memory import InMemoryDatabase


def test_cannot_change_artifact_digest_of_released_version():
    store = InMemoryDatabase()
    with store.transaction() as tx:
        tx.insert_portal_system(SystemRow(id="sys-immu", unit="Core", description="desc", owner="Dev", status="healthy"))
        tx.insert_portal_module(
            ModuleRow(
                id="mod-immu",
                system_id="sys-immu",
                name="Module Immu",
                module_type="service",
                description="desc",
                runtime="docker",
                application_id=uuid4(),
                deployment_config=[],
                pipeline_config={},
            )
        )
        tx.insert_portal_version(
            VersionRow(
                module_id="mod-immu",
                version="v1.0.0",
                metadata={"artifactDigest": "sha256:1111111111111111111111111111111111111111111111111111111111111111", "createdBy": "alice"},
            )
        )

    # Attempting to re-register with different artifactDigest must raise VersionConflict
    with pytest.raises(VersionConflict) as exc_info:
        with store.transaction() as tx:
            tx.insert_portal_version(
                VersionRow(
                    module_id="mod-immu",
                    version="v1.0.0",
                    metadata={"artifactDigest": "sha256:2222222222222222222222222222222222222222222222222222222222222222", "createdBy": "bob"},
                )
            )
    assert "already exists" in str(exc_info.value)


def test_reregistering_identical_version_is_idempotent():
    store = InMemoryDatabase()
    with store.transaction() as tx:
        tx.insert_portal_system(SystemRow(id="sys-idem", unit="Core", description="desc", owner="Dev", status="healthy"))
        tx.insert_portal_module(
            ModuleRow(
                id="mod-idem",
                system_id="sys-idem",
                name="Module Idem",
                module_type="service",
                description="desc",
                runtime="docker",
                application_id=None,
                deployment_config=[],
                pipeline_config={},
            )
        )
        v_row = VersionRow(
            module_id="mod-idem",
            version="v1.0.0",
            metadata={"artifactDigest": "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "createdBy": "alice"},
        )
        tx.insert_portal_version(v_row)

    # Re-registering with identical payload should succeed without error
    with store.transaction() as tx:
        tx.insert_portal_version(v_row)
        res = tx.portal_version("mod-idem", "v1.0.0")
        assert res is not None
        assert res.metadata["artifactDigest"] == "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"


def test_ci_report_is_stored_separately_and_does_not_mutate_version_row():
    store = InMemoryDatabase()
    with store.transaction() as tx:
        tx.insert_portal_system(SystemRow(id="sys-ci", unit="Core", description="desc", owner="Dev", status="healthy"))
        tx.insert_portal_module(
            ModuleRow(
                id="mod-ci",
                system_id="sys-ci",
                name="Module CI",
                module_type="service",
                description="desc",
                runtime="docker",
                application_id=None,
                deployment_config=[],
                pipeline_config={},
            )
        )
        tx.insert_portal_version(
            VersionRow(
                module_id="mod-ci",
                version="v1.0.0",
                metadata={"artifactDigest": "sha256:3333333333333333333333333333333333333333333333333333333333333333", "createdBy": "pipeline"},
            )
        )

        # Record CI report separately
        report = {"testPassCount": 50, "testFailCount": 0, "coveragePercentage": 99.0}
        tx.insert_version_ci_report("mod-ci", "v1.0.0", report, recorded_by="jenkins-worker")

        # Read back version and verify report is joined dynamically
        v = tx.portal_version("mod-ci", "v1.0.0")
        assert v is not None
        assert v.metadata["ciReport"]["coveragePercentage"] == 99.0
        assert v.metadata["artifactDigest"] == "sha256:3333333333333333333333333333333333333333333333333333333333333333"


@pytest.mark.skipif(not os.getenv("NETCI_TEST_DATABASE_URL"), reason="PostgreSQL required")
def test_postgres_concurrent_version_registration_conflict():
    from app.store.postgres import PostgresDatabase
    db = PostgresDatabase(os.getenv("NETCI_TEST_DATABASE_URL"))
    mod_id = f"mod-race-{uuid4().hex[:6]}"
    sys_id = f"sys-race-{uuid4().hex[:6]}"
    
    with db.transaction() as tx:
        tx.insert_portal_system(SystemRow(id=sys_id, unit="Core", description="desc", owner="Dev", status="healthy"))
        tx.insert_portal_module(
            ModuleRow(
                id=mod_id,
                system_id=sys_id,
                name="Module Race",
                module_type="service",
                description="desc",
                runtime="docker",
                application_id=None,
                deployment_config=[],
                pipeline_config={},
            )
        )
        tx.insert_portal_version(
            VersionRow(
                module_id=mod_id,
                version="v1.0.0",
                metadata={"artifactDigest": "sha256:winner", "createdBy": "agent-1"},
            )
        )

    # Second transaction attempting to insert different digest for same module + version must raise VersionConflict
    with pytest.raises(VersionConflict):
        with db.transaction() as tx:
            tx.insert_portal_version(
                VersionRow(
                    module_id=mod_id,
                    version="v1.0.0",
                    metadata={"artifactDigest": "sha256:loser", "createdBy": "agent-2"},
                )
            )


@pytest.mark.skipif(not os.getenv("NETCI_TEST_DATABASE_URL"), reason="PostgreSQL required")
def test_backup_fails_if_critical_table_is_missing(tmp_path):
    """Test that restore verify fails when a critical table is omitted."""
    from scripts.netci_backup import verify, load_manifest, MANIFEST_NAME
    import argparse

    db_url = os.getenv("NETCI_TEST_DATABASE_URL")
    backup_dir = tmp_path / "backup_test"
    backup_dir.mkdir()

    # Run backup create
    from scripts.netci_backup import create
    create_args = argparse.Namespace(database_url=db_url, output=str(backup_dir))
    create(create_args)

    created_dirs = [d for d in backup_dir.iterdir() if d.is_dir()]
    assert created_dirs, "Backup directory not found"
    target = created_dirs[0]

    # Tamper manifest to require a non-existent critical table
    manifest_path = target / MANIFEST_NAME
    manifest = load_manifest(target)
    manifest["criticalTables"].append("security_evidence_missing")
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    verify_args = argparse.Namespace(
        database_url=db_url,
        input=str(target),
        scratch_database=f"test_scratch_{uuid4().hex[:6]}",
        keep=False,
    )

    with pytest.raises(SystemExit) as exc:
        verify(verify_args)
    assert exc.value.code != 0
