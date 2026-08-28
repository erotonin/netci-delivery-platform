#!/usr/bin/env python3
"""Systemd E2E gate: a signed binary released, restarted, health-checked and rolled back.

The binary path is where "immutable artifact" means a content hash rather than a
registry digest, and where rollback is a symlink flip plus a restart. This gate runs
that for real: the Go sample is built, scanned, signed and verified, netCI approves it,
and the checked-in Ansible systemd adapter installs the unit and restarts the service.

    python scripts/gate_e2e_systemd.py --api-url http://127.0.0.1:8000 --scope user

`--scope user` installs into the invoking user's systemd instance, which needs no root
and touches nothing outside $HOME -- use it on a shared machine. `--scope system` is the
documented VM topology.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from netci_gates.client import NetciClient  # noqa: E402
from netci_gates.evidence import (  # noqa: E402
    PROJECT_ROOT,
    EvidenceRecorder,
    GateFailure,
    require_tools,
    run_gate,
)

WORK_DIR = PROJECT_ROOT / ".netci-gate" / "e2e-systemd"
COLLECTIONS = PROJECT_ROOT / ".netci-gate" / "collections"
APP_DIR = PROJECT_ROOT / "sample-apps" / "hello-systemd-go"
CI_DIR = PROJECT_ROOT / "templates" / "systemd-ansible-ci-cd-v1" / "scripts" / "ci"


def severity_counts(report: dict) -> dict[str, int]:
    counts = {"critical": 0, "high": 0, "medium": 0}
    for result in report.get("Results") or []:
        for vulnerability in result.get("Vulnerabilities") or []:
            severity = str(vulnerability.get("Severity", "")).lower()
            if severity in counts:
                counts[severity] += 1
    return counts


def wait_for_health(url: str, *, attempts: int = 30, delay: float = 1.0) -> dict:
    last: Exception | None = None
    for _ in range(attempts):
        try:
            with urllib.request.urlopen(url, timeout=5) as response:
                return json.loads(response.read() or b"{}")
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
            last = exc
            time.sleep(delay)
    raise GateFailure(f"{url} never became healthy: {last}")


def build_release(recorder: EvidenceRecorder, version: str, keys: tuple[str, str]) -> dict[str, object]:
    """Run the systemd template's CI chain for one version and return its evidence."""

    output = WORK_DIR / version
    output.mkdir(parents=True, exist_ok=True)
    env = {"NETCI_OUTPUT_DIR": str(output), "VERSION": version, "NETCI_APP_DIR": str(APP_DIR)}
    private_key, public_key = keys

    recorder.run(f"{version}:unit-test", ["bash", str(CI_DIR / "test.sh")], env=env)
    recorder.run(f"{version}:build", ["bash", str(CI_DIR / "build.sh")], env=env)
    digest = (output / "artifact-digest.txt").read_text(encoding="utf-8").strip()
    recorder.check(f"{version}: the build produced a sha256 content digest", digest.startswith("sha256:"), detail=digest)

    recorder.run(f"{version}:sbom", ["bash", str(CI_DIR / "sbom.sh")], env=env)
    sbom = json.loads((output / "sbom.json").read_text(encoding="utf-8"))
    recorder.check(f"{version}: Syft produced a CycloneDX SBOM", sbom.get("bomFormat") == "CycloneDX")

    recorder.run(f"{version}:vulnerability-scan", ["bash", str(CI_DIR / "scan.sh")], env=env)
    counts = severity_counts(json.loads((output / "scan-report.json").read_text(encoding="utf-8")))
    recorder.record(f"{version}:scan-counts", counts)
    recorder.check_equal(f"{version}: no fixable critical vulnerabilities", counts["critical"], 0)
    recorder.check_equal(f"{version}: no fixable high vulnerabilities", counts["high"], 0)

    recorder.run(
        f"{version}:sign",
        ["bash", str(CI_DIR / "sign.sh")],
        env={**env, "COSIGN_KEY_REF": private_key, "COSIGN_PASSWORD": ""},
    )
    binary = APP_DIR / "dist" / f"hello-systemd-{version}"
    verify = recorder.run(
        f"{version}:verify-signature",
        [
            "cosign", "verify-blob", "--key", public_key,
            "--bundle", str(output / "signature.bundle.json"),
            "--insecure-ignore-tlog=true", str(binary),
        ],
        env={"COSIGN_PASSWORD": ""},
    )
    recorder.check(f"{version}: Cosign verifies the built binary", verify.returncode == 0)

    # Keep a copy: the next build overwrites dist/ for a version that reuses the name.
    staged = output / f"hello-systemd-{version}"
    shutil.copy2(binary, staged)

    return {
        "artifactDigest": digest,
        "artifactRef": f"file://{staged}",
        "sbom": {"generatedBy": "syft", "location": str(output / "sbom.json"), "format": "cyclonedx-json"},
        "vulnerabilityScan": {
            "scanner": "trivy",
            "status": "passed",
            "critical": counts["critical"],
            "high": counts["high"],
            "medium": counts["medium"],
            "reportLocation": str(output / "scan-report.json"),
        },
        "signature": {
            "provider": "cosign",
            "verified": True,
            "certificateIdentity": "netci-local-gate-key",
            "bundleLocation": str(output / "signature.bundle.json"),
        },
        "_binary": str(staged),
        "_version": version,
    }


def deploy(recorder: EvidenceRecorder, release: dict[str, object], *, step: str) -> None:
    context = recorder.context
    extra_vars = {
        "netci_action": "deploy",
        "app_name": context["appName"],
        "app_root": context["appRoot"],
        "app_port": context["port"],
        "release_version": release["_version"],
        "artifact_path": release["_binary"],
        "artifact_sha256": str(release["artifactDigest"]).removeprefix("sha256:"),
        "health_url": f"http://127.0.0.1:{context['port']}/healthz",
        "systemd_scope": context["scope"],
        "netci_become": context["scope"] == "system",
        "target_environment": "staging",
    }
    recorder.run(
        step,
        [
            str(PROJECT_ROOT / ".venv/bin/ansible-playbook"),
            "-i", str(PROJECT_ROOT / "deploy/ansible/inventories/localhost.ini"),
            str(PROJECT_ROOT / "deploy/ansible/playbooks/deploy-systemd.yml"),
            "--extra-vars", json.dumps(extra_vars, sort_keys=True),
        ],
        env={
            "ANSIBLE_COLLECTIONS_PATH": str(COLLECTIONS),
            "ANSIBLE_HOST_KEY_CHECKING": "False",
            "XDG_RUNTIME_DIR": os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"),
        },
        detail={"extraVars": extra_vars},
        timeout=600,
    )


def current_release_target(app_root: Path) -> str:
    link = app_root / "current"
    return os.readlink(link) if link.is_symlink() else ""


def gate(recorder: EvidenceRecorder) -> None:
    require_tools(recorder, "go", "syft", "trivy", "cosign", "systemctl")
    context = recorder.context
    client = NetciClient(context["apiUrl"], context["pipelineApiKey"])

    health = client.health()
    recorder.record("api-health", health)
    recorder.check_equal("the netCI API is healthy", health.get("status"), "ok")

    if WORK_DIR.exists():
        shutil.rmtree(WORK_DIR)
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    keys = generate_key(recorder)

    first = build_release(recorder, "v0.1.0", keys)
    second = build_release(recorder, "v0.2.0", keys)
    recorder.check(
        "two versions of the binary have different content digests",
        first["artifactDigest"] != second["artifactDigest"],
        detail={"v0.1.0": first["artifactDigest"], "v0.2.0": second["artifactDigest"]},
    )

    name = f"gate-systemd-{uuid.uuid4().hex[:6]}"
    application = client.create_application(
        {
            "name": name,
            "repositoryUrl": f"https://github.com/example/{name}",
            "pipelineTemplate": "systemd-ansible-ci-cd-v1",
            "runtime": "systemd",
        },
        idempotency_key=f"gate-app-{name}",
    )
    recorder.check_equal(
        "the systemd template selects the binary stage list, not the container one",
        application["stages"],
        ["checkout", "unit-test", "build", "publish", "deploy", "health-check"],
    )

    run = client.start_pipeline(
        application["id"], {"commitSha": "sysd111", "environment": "prod"}, idempotency_key=f"gate-run-{name}-1"
    )
    client.ci_result(run["id"], {"status": "running"})
    decision = client.publish_evidence(run["id"], {k: v for k, v in first.items() if not k.startswith("_")})
    recorder.record("v0.1.0:policy-decision", decision)
    recorder.check_equal("the signed binary satisfies the supply-chain policy", decision["decision"], "allow")

    ci = client.ci_result(run["id"], {"status": "succeeded", "artifactDigest": first["artifactDigest"]})
    deployment = ci["deployment"]
    recorder.check_equal("production waits for approval", deployment["status"], "pending_approval")
    client.approve(deployment["id"])

    app_root = Path(context["appRoot"])
    deploy(recorder, first, step="deploy:v0.1.0")
    health_url = f"http://127.0.0.1:{context['port']}/healthz"
    running = wait_for_health(health_url)
    recorder.record("v0.1.0:service-health", running)
    recorder.check_equal("the systemd service reports healthy", running.get("status"), "ok")
    recorder.check_equal(
        "the current symlink points at the released version",
        current_release_target(app_root),
        str(app_root / "releases" / "v0.1.0"),
    )
    installed = recorder.run(
        "v0.1.0:verify-installed-digest",
        ["sha256sum", str(app_root / "current" / context["appName"])],
    )
    recorder.check(
        "the installed binary is byte-identical to the approved artifact",
        installed.stdout.split()[0] == str(first["artifactDigest"]).removeprefix("sha256:"),
        detail=installed.stdout.strip(),
    )
    client.deployment_result(deployment["id"], "healthy", f"verified at {health_url}")

    # ------------------------------------------------------- promote, then roll back
    second_run = client.start_pipeline(
        application["id"], {"commitSha": "sysd222", "environment": "prod"}, idempotency_key=f"gate-run-{name}-2"
    )
    client.ci_result(second_run["id"], {"status": "running"})
    client.publish_evidence(second_run["id"], {k: v for k, v in second.items() if not k.startswith("_")})
    second_ci = client.ci_result(
        second_run["id"], {"status": "succeeded", "artifactDigest": second["artifactDigest"]}
    )
    second_deployment = second_ci["deployment"]
    client.approve(second_deployment["id"])
    deploy(recorder, second, step="deploy:v0.2.0")
    wait_for_health(health_url)
    recorder.check_equal(
        "the symlink moved to the promoted release",
        current_release_target(app_root),
        str(app_root / "releases" / "v0.2.0"),
    )
    client.deployment_result(second_deployment["id"], "healthy", "v0.2.0 healthy")

    rolled_back = client.rollback(second_deployment["id"], first["artifactDigest"], "systemd rollback drill")
    recorder.check_equal("netCI records the rollback", rolled_back["status"], "rolled_back")
    deploy(recorder, first, step="deploy:rollback")
    restored = wait_for_health(health_url)
    recorder.record("rollback:service-health", restored)
    recorder.check_equal(
        "the symlink is back on the previous release",
        current_release_target(app_root),
        str(app_root / "releases" / "v0.1.0"),
    )
    recorder.check_equal("the rolled-back service is healthy", restored.get("status"), "ok")

    status = recorder.run(
        "collect-unit-status",
        ["systemctl", "--user" if context["scope"] == "user" else "--system", "status", context["appName"], "--no-pager"],
        expect_success=False,
    )
    recorder.record("unit-status", {"stdout": status.stdout[-3000:]})
    recorder.check("systemd reports the unit as active", "active (running)" in status.stdout, detail=status.stdout[-400:])


def generate_key(recorder: EvidenceRecorder) -> tuple[str, str]:
    key_dir = PROJECT_ROOT / ".netci-gate" / "keys"
    key_dir.mkdir(parents=True, exist_ok=True)
    private_key = key_dir / "cosign.key"
    public_key = key_dir / "cosign.pub"
    if not private_key.is_file():
        recorder.run(
            "generate-cosign-key",
            ["cosign", "generate-key-pair", "--output-key-prefix", "cosign"],
            cwd=key_dir,
            env={"COSIGN_PASSWORD": ""},
        )
    return str(private_key), str(public_key)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--api-url", default=os.getenv("NETCI_API_URL", "http://127.0.0.1:8000"))
    parser.add_argument("--pipeline-api-key", default=os.getenv("NETCI_PIPELINE_API_KEY", "netci-local-pipeline-key"))
    parser.add_argument("--port", type=int, default=int(os.getenv("NETCI_LAB_SYSTEMD_PORT", "18082")))
    parser.add_argument(
        "--scope",
        default=os.getenv("NETCI_LAB_SYSTEMD_SCOPE", "user"),
        choices=["user", "system"],
        help="user installs into the invoking user's systemd instance and needs no root",
    )
    arguments = parser.parse_args()

    recorder = EvidenceRecorder(
        "e2e-systemd",
        description="Signed Go binary released through netCI to a systemd unit: install, restart, "
        "health check, promote and roll back by symlink.",
    )
    recorder.context.update(
        {
            "apiUrl": arguments.api_url,
            "pipelineApiKey": arguments.pipeline_api_key,
            "appName": "netci-gate-hello-systemd",
            "appRoot": str(PROJECT_ROOT / ".netci-gate" / "deploy" / "systemd"),
            "port": arguments.port,
            "scope": arguments.scope,
        }
    )
    return run_gate(recorder, gate)


if __name__ == "__main__":
    raise SystemExit(main())
