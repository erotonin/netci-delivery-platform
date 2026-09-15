#!/usr/bin/env python3
"""Container E2E gate: one artifact from source to a running, health-checked service.

Nothing here is simulated. The gate builds the sample application, pushes it to a real
registry, generates a real SBOM, scans and signs it, drives the netCI API exactly as the
Portal and Jenkins do, then hands the digest to the Ansible Docker adapter, which starts
the container and verifies its health endpoint. Finally it forces a rollback and checks
the service returns to the previous digest.

    python scripts/gate_e2e_container.py --api-url http://127.0.0.1:8000

Preconditions: an OCI registry reachable at --registry (see `make lab-up`), the netCI API
running, and docker/syft/trivy/cosign/ansible-playbook installed.
"""

from __future__ import annotations

import argparse
import importlib.util
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

WORK_DIR = PROJECT_ROOT / ".netci-gate" / "e2e-container"
COLLECTIONS = PROJECT_ROOT / ".netci-gate" / "collections"


# The same evidence shape `scripts/netci_callback.py` publishes, imported rather than
# restated. A gate that builds its own payload proves the policy works on a payload no
# build ever sends -- and the identifiers are what a vulnerability exception matches on.
_CALLBACK_SPEC = importlib.util.spec_from_file_location(
    "netci_callback_for_gate", PROJECT_ROOT / "scripts" / "netci_callback.py"
)
_CALLBACK = importlib.util.module_from_spec(_CALLBACK_SPEC)
_CALLBACK_SPEC.loader.exec_module(_CALLBACK)

severity_counts = _CALLBACK.trivy_counts
scan_findings = _CALLBACK.trivy_findings

GOLDEN_BASE = "netci/python-base:3.12-alpine"
UPSTREAM_BASE = "python:3.12-alpine"


def build_golden_base(recorder: EvidenceRecorder) -> str:
    """Build the patched base the application image is layered on.

    `sample-apps/base-python` runs `apk upgrade`, which is the project's answer to a
    pinned base accumulating fixable CVEs: patch once, centrally, rather than weakening
    the gate or patching in every application Dockerfile. Building the application
    straight on the upstream image instead -- which this gate used to do -- meant the
    golden base existed but nothing used it, and the vulnerability gate failed the moment
    upstream published a fix the pinned tag did not have.
    """

    recorder.run(
        "golden-base:build",
        [
            "docker", "build", "--network", "host", "--pull",
            "--build-arg", f"PYTHON_IMAGE={UPSTREAM_BASE}",
            "-t", GOLDEN_BASE, str(PROJECT_ROOT / "sample-apps" / "base-python"),
        ],
    )
    return GOLDEN_BASE



def http_json(url: str, timeout: float = 5.0) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.loads(response.read() or b"{}")


def wait_for_health(url: str, *, attempts: int = 30, delay: float = 1.0) -> dict:
    last: Exception | None = None
    for _ in range(attempts):
        try:
            return http_json(url)
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
            last = exc
            time.sleep(delay)
    raise GateFailure(f"{url} never became healthy: {last}")


def build_and_publish(
    recorder: EvidenceRecorder,
    *,
    version: str,
    registry: str,
    repository: str,
    cosign_key: tuple[str, str],
) -> dict[str, object]:
    """Run the real CI chain for one version and return its evidence payload."""

    output = WORK_DIR / version
    output.mkdir(parents=True, exist_ok=True)
    context = PROJECT_ROOT / "sample-apps" / "hello-container"
    tag = f"{repository}:{version}"
    private_key, public_key = cosign_key

    recorder.run(
        f"{version}:unit-test",
        ["bash", str(PROJECT_ROOT / "templates/container-ci-cd-v1/scripts/ci/test.sh")],
        env={"NETCI_OUTPUT_DIR": str(output), "NETCI_APP_DIR": str(context)},
    )
    recorder.run(
        f"{version}:build",
        [
            "docker", "build", "--network", "host",
            "--build-arg", f"PYTHON_IMAGE={GOLDEN_BASE}",
            "--label", f"org.netci.version={version}",
            "-t", tag, str(context),
        ],
    )
    # `docker push` reports the registry digest: the artifact's immutable identity.
    push = recorder.run(f"{version}:push", ["docker", "push", tag])
    digest = ""
    for line in (push.stdout + push.stderr).splitlines():
        if "digest: sha256:" in line:
            digest = line.split("digest:", 1)[1].strip().split()[0]
            break
    recorder.check(f"{version}: the registry returned an immutable digest", digest.startswith("sha256:"), detail=digest)
    reference = f"{repository}@{digest}"
    (output / "artifact-digest.txt").write_text(digest + "\n", encoding="utf-8")
    (output / "artifact-ref.txt").write_text(reference + "\n", encoding="utf-8")

    sbom_path = output / "sbom.json"
    recorder.run(
        f"{version}:sbom",
        ["syft", f"registry:{reference}", "--output", f"cyclonedx-json={sbom_path}"],
        env={"SYFT_REGISTRY_INSECURE_USE_HTTP": "true", "SYFT_REGISTRY_INSECURE_SKIP_TLS_VERIFY": "true"},
    )
    sbom = json.loads(sbom_path.read_text(encoding="utf-8"))
    recorder.check(f"{version}: Syft produced a CycloneDX SBOM", sbom.get("bomFormat") == "CycloneDX")

    scan_path = output / "scan-report.json"
    recorder.run(
        f"{version}:vulnerability-scan",
        [
            "trivy", "image", "--severity", "HIGH,CRITICAL", "--ignore-unfixed",
            "--scanners", "vuln", "--format", "json", "--output", str(scan_path), reference,
        ],
        env={"TRIVY_INSECURE": "true", "TRIVY_NON_SSL": "true"},
    )
    scan_report = json.loads(scan_path.read_text(encoding="utf-8"))
    counts = severity_counts(scan_report)
    findings = scan_findings(scan_report)
    recorder.record(f"{version}:scan-counts", counts)

    # An image signature lives in the registry beside the image; the durable evidence
    # is the verification result, which is what netCI's policy actually relies on.
    bundle = output / "signature.bundle.json"
    recorder.run(
        f"{version}:sign",
        [
            "cosign", "sign", "--yes", "--key", private_key,
            "--allow-insecure-registry", "--tlog-upload=false", reference,
        ],
        env={"COSIGN_PASSWORD": ""},
    )
    verify = recorder.run(
        f"{version}:verify-signature",
        [
            "cosign", "verify", "--key", public_key, "--allow-insecure-registry",
            "--insecure-ignore-tlog=true", "--output", "json", reference,
        ],
        env={"COSIGN_PASSWORD": ""},
    )
    bundle.write_text(verify.stdout, encoding="utf-8")
    recorder.check(
        f"{version}: Cosign verifies the published image against the signing key",
        verify.returncode == 0 and bundle.stat().st_size > 0,
    )

    return {
        "artifactDigest": digest,
        "artifactRef": reference,
        "sbom": {"generatedBy": "syft", "location": str(sbom_path), "format": "cyclonedx-json"},
        "vulnerabilityScan": {
            "scanner": "trivy",
            "status": "passed" if counts["critical"] == 0 and counts["high"] == 0 else "failed",
            "critical": counts["critical"],
            "findings": findings,
            "high": counts["high"],
            "medium": counts["medium"],
            "reportLocation": str(scan_path),
        },
        "signature": {
            "provider": "cosign",
            "verified": True,
            "certificateIdentity": "netci-local-gate-key",
            "bundleLocation": str(bundle),
        },
    }


def deploy(recorder: EvidenceRecorder, name: str, digest: str, *, step: str) -> None:
    """Run the checked-in Ansible Docker adapter -- the same one Temporal invokes."""

    context = recorder.context
    extra_vars = {
        "netci_action": "deploy",
        "app_name": context["appName"],
        "app_root": context["appRoot"],
        "image_repository": context["repository"],
        "artifact_digest": digest,
        "host_port": context["port"],
        "network_mode": context["networkMode"],
        "target_environment": "staging",
    }
    recorder.run(
        step,
        [
            str(PROJECT_ROOT / ".venv/bin/ansible-playbook"),
            "-i", str(PROJECT_ROOT / "deploy/ansible/inventories/localhost.ini"),
            # One logical target, as netCI itself passes: the inventory lists this host
            # once per environment under its DCIM name.
            "--limit", "netci-local-docker-staging",
            str(PROJECT_ROOT / "deploy/ansible/playbooks/deploy-docker.yml"),
            "--extra-vars", json.dumps(extra_vars, sort_keys=True),
        ],
        env={"ANSIBLE_COLLECTIONS_PATH": str(COLLECTIONS), "ANSIBLE_HOST_KEY_CHECKING": "False"},
        detail={"extraVars": extra_vars, "name": name},
        timeout=600,
    )


def gate(recorder: EvidenceRecorder) -> None:
    require_tools(recorder, "docker", "syft", "trivy", "cosign")
    context = recorder.context
    client = NetciClient(context["apiUrl"], context["pipelineApiKey"])

    health = client.health()
    recorder.record("api-health", health)
    recorder.check_equal("the netCI API is healthy", health.get("status"), "ok")
    recorder.check(
        "netCI is backed by a database, not process memory",
        health["dependencies"]["deliveryPersistence"] == "ok",
        detail=health["dependencies"],
    )

    # Before anything is built: the application layers on this, so a stale base is a
    # vulnerability-gate failure attributed to the application.
    build_golden_base(recorder)

    registry_url = f"http://{context['registry']}/v2/"
    recorder.record("registry-probe", {"url": registry_url, "reachable": True})
    try:
        urllib.request.urlopen(registry_url, timeout=5)
    except Exception as exc:
        raise GateFailure(f"the OCI registry at {context['registry']} is not reachable: {exc}")

    if WORK_DIR.exists():
        shutil.rmtree(WORK_DIR)
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    cosign_key = generate_key(recorder)

    # ------------------------------------------------------------- CI for v1 and v2
    first = build_and_publish(
        recorder, version="v1", registry=context["registry"], repository=context["repository"], cosign_key=cosign_key
    )
    second = build_and_publish(
        recorder, version="v2", registry=context["registry"], repository=context["repository"], cosign_key=cosign_key
    )
    recorder.check(
        "two builds of different source produce two different digests",
        first["artifactDigest"] != second["artifactDigest"],
        detail={"v1": first["artifactDigest"], "v2": second["artifactDigest"]},
    )

    # --------------------------------------------------------- netCI drives delivery
    name = f"gate-e2e-{uuid.uuid4().hex[:6]}"
    application = client.create_application(
        {
            "name": name,
            "repositoryUrl": f"https://github.com/example/{name}",
            "pipelineTemplate": "container-ci-cd-v1",
            "runtime": "docker",
        },
        idempotency_key=f"gate-app-{name}",
    )
    recorder.context["applicationId"] = application["id"]

    run = client.start_pipeline(
        application["id"],
        {"commitSha": "e2e1111", "environment": "prod"},
        idempotency_key=f"gate-run-{name}-v1",
    )
    recorder.check_equal("a new run starts queued", run["status"], "queued")

    replay = client.start_pipeline(
        application["id"],
        {"commitSha": "e2e1111", "environment": "prod"},
        idempotency_key=f"gate-run-{name}-v1",
    )
    recorder.check_equal("replaying the same idempotency key returns the same run", replay["id"], run["id"])

    client.ci_result(run["id"], {"status": "running"})
    decision = client.publish_evidence(run["id"], first)
    recorder.record("v1:policy-decision", decision)
    recorder.check_equal("the v1 artifact satisfies the supply-chain policy", decision["decision"], "allow")

    ci = client.ci_result(run["id"], {"status": "succeeded", "artifactDigest": first["artifactDigest"]})
    deployment = ci["deployment"]
    recorder.check_equal("a production deployment waits for approval", deployment["status"], "pending_approval")
    recorder.check_equal("the pipeline waits for approval too", ci["pipelineRun"]["status"], "waiting_approval")

    approved = client.approve(deployment["id"])
    recorder.check_equal("approval moves the deployment to deploying", approved["status"], "deploying")
    # Attributed to the verified caller, not to a name in the request body. Reading it
    # back from /me keeps the assertion meaningful in every auth mode: "anonymous" on a
    # loopback lab, a real subject once NETCI_AUTH_MODE is token or oidc.
    approver = client.whoami()["principal"]["subject"]
    recorder.check_equal("approval is attributed to the authenticated caller", approved["approvedBy"], approver)

    # ------------------------------------------------------------- real deployment
    deploy(recorder, "v1", first["artifactDigest"], step="deploy:v1")
    health_url = f"http://127.0.0.1:{context['port']}/healthz"
    running = wait_for_health(health_url)
    recorder.record("v1:service-health", running)
    recorder.check_equal("the deployed service reports healthy", running.get("status"), "ok")
    recorder.check_equal(
        "the running service reports the digest that was approved",
        running.get("version"),
        first["artifactDigest"],
    )
    client.deployment_result(deployment["id"], "healthy", f"verified at {health_url}")
    recorder.check_equal(
        "the pipeline completes once the deployment is healthy",
        client.pipeline(run["id"])["status"],
        "succeeded",
    )

    # ------------------------------------------------ promote v2, then roll it back
    second_run = client.start_pipeline(
        application["id"],
        {"commitSha": "e2e2222", "environment": "prod"},
        idempotency_key=f"gate-run-{name}-v2",
    )
    client.ci_result(second_run["id"], {"status": "running"})
    client.publish_evidence(second_run["id"], second)
    second_ci = client.ci_result(
        second_run["id"], {"status": "succeeded", "artifactDigest": second["artifactDigest"]}
    )
    second_deployment = second_ci["deployment"]
    client.approve(second_deployment["id"])
    deploy(recorder, "v2", second["artifactDigest"], step="deploy:v2")
    promoted = wait_for_health(health_url)
    recorder.check_equal(
        "the promoted release runs the second digest", promoted.get("version"), second["artifactDigest"]
    )
    client.deployment_result(second_deployment["id"], "healthy", "v2 healthy")

    rolled_back = client.rollback(second_deployment["id"], first["artifactDigest"], "gate rollback drill")
    recorder.check(
        "netCI records the rollback",
        rolled_back["status"] in ("rolled_back", "rollback_in_progress"),
        detail=rolled_back["status"],
    )
    recorder.check_equal(
        "the rollback keeps the superseded digest for audit",
        rolled_back["previousArtifactDigest"],
        second["artifactDigest"],
    )
    deploy(recorder, "v1-rollback", first["artifactDigest"], step="deploy:rollback")
    restored = wait_for_health(health_url)
    recorder.check_equal(
        "the service is serving the rolled-back digest again", restored.get("version"), first["artifactDigest"]
    )

    # ------------------------------------------------------------- source events
    events = client.delivery_events(application["id"])
    recorder.record("delivery-events", events)
    kinds = [item["eventType"] for item in events["items"]]
    recorder.check("a commit event was recorded for each run", kinds.count("commit") == 2, detail=kinds)
    recorder.check(
        "each production deployment produced a deployment event",
        kinds.count("deployment") >= 2,
        detail=kinds,
    )
    dora = client.dora(application["id"])
    recorder.record("dora", dora)
    recorder.check(
        "DORA is projected from the events this gate produced",
        dora["sourceEvents"] == len(events["items"]),
        detail={"sourceEvents": dora["sourceEvents"], "events": len(events["items"])},
    )
    recorder.check("deployment frequency is non-zero after two releases", dora["metrics"]["deploymentFrequency"] > 0)


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
    parser.add_argument("--registry", default=os.getenv("NETCI_REGISTRY", "127.0.0.1:5000"))
    parser.add_argument("--port", type=int, default=int(os.getenv("NETCI_LAB_APP_PORT", "18081")))
    parser.add_argument(
        "--network-mode",
        default=os.getenv("NETCI_LAB_NETWORK_MODE", "bridge"),
        choices=["bridge", "host"],
        help="use host where the Docker daemon has no bridge network",
    )
    arguments = parser.parse_args()

    recorder = EvidenceRecorder(
        "e2e-container",
        description="Source to running container: real build, registry digest, SBOM, scan, signature, "
        "approval, Ansible deploy, health check and rollback.",
    )
    recorder.context.update(
        {
            "apiUrl": arguments.api_url,
            "pipelineApiKey": arguments.pipeline_api_key,
            "registry": arguments.registry,
            "repository": f"{arguments.registry}/hello-container",
            "appName": "netci-gate-hello-container",
            "appRoot": str(PROJECT_ROOT / ".netci-gate" / "deploy" / "docker"),
            "port": arguments.port,
            "networkMode": arguments.network_mode,
        }
    )
    return run_gate(recorder, gate)


if __name__ == "__main__":
    raise SystemExit(main())
