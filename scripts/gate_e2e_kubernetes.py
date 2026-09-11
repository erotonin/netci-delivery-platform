#!/usr/bin/env python3
"""Kubernetes E2E gate: the same digest promoted onto kind through Helm, then rolled back.

The Kubernetes path is where "build once, promote many" has to be demonstrated rather
than asserted: the running pod's image is read back from the cluster and compared to the
digest netCI approved. Rollback uses a Helm revision, not a rebuild.

    python scripts/gate_e2e_kubernetes.py --api-url http://127.0.0.1:8000 --registry netci-kind-registry:5000

The registry must be resolvable from inside the cluster; `make kind-up` and
`infra/kind/local-registry.sh` set that up.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import subprocess
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

WORK_DIR = PROJECT_ROOT / ".netci-gate" / "e2e-kubernetes"
COLLECTIONS = PROJECT_ROOT / ".netci-gate" / "collections"
CHART = PROJECT_ROOT / "deploy" / "helm" / "sample-kubernetes-app"
APP_DIR = PROJECT_ROOT / "sample-apps" / "hello-kubernetes"
RELEASE = "netci-gate-hello-kubernetes"
NAMESPACE = "staging"


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



def kubectl_json(recorder: EvidenceRecorder, name: str, *arguments: str) -> dict:
    result = recorder.run(name, ["kubectl", "--context", recorder.context["kubeContext"], *arguments, "-o", "json"])
    return json.loads(result.stdout or "{}")


def build_and_publish(recorder: EvidenceRecorder, version: str, keys: tuple[str, str]) -> dict[str, object]:
    output = WORK_DIR / version
    output.mkdir(parents=True, exist_ok=True)
    # The gate pushes over an address the host can reach and deploys over the address
    # the cluster resolves. Both name the same repository on the same registry, so the
    # digest -- the thing that actually identifies the artifact -- is identical.
    repository = recorder.context["pushRepository"]
    tag = f"{repository}:{version}"
    private_key, public_key = keys

    recorder.run(
        f"{version}:build",
        [
            "docker", "build", "--network", "host",
            "--build-arg", f"PYTHON_IMAGE={GOLDEN_BASE}",
            "--label", f"org.netci.version={version}",
            "-t", tag, str(APP_DIR),
        ],
    )
    push = recorder.run(f"{version}:push", ["docker", "push", tag])
    digest = ""
    for line in (push.stdout + push.stderr).splitlines():
        if "digest: sha256:" in line:
            digest = line.split("digest:", 1)[1].strip().split()[0]
            break
    recorder.check(f"{version}: the registry returned an immutable digest", digest.startswith("sha256:"), detail=digest)
    reference = f"{repository}@{digest}"

    sbom_path = output / "sbom.json"
    recorder.run(
        f"{version}:sbom",
        ["syft", f"registry:{reference}", "--output", f"cyclonedx-json={sbom_path}"],
        env={"SYFT_REGISTRY_INSECURE_USE_HTTP": "true", "SYFT_REGISTRY_INSECURE_SKIP_TLS_VERIFY": "true"},
    )
    recorder.check(
        f"{version}: Syft produced a CycloneDX SBOM",
        json.loads(sbom_path.read_text(encoding="utf-8")).get("bomFormat") == "CycloneDX",
    )

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

    recorder.run(
        f"{version}:sign",
        ["cosign", "sign", "--yes", "--key", private_key, "--allow-insecure-registry", "--tlog-upload=false", reference],
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
    bundle = output / "signature.bundle.json"
    bundle.write_text(verify.stdout, encoding="utf-8")
    recorder.check(f"{version}: Cosign verifies the published image", verify.returncode == 0)

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


def helm_deploy(recorder: EvidenceRecorder, digest: str, *, step: str) -> None:
    """Run the checked-in Ansible/Helm adapter, which asserts the running digest itself."""

    extra_vars = {
        "netci_action": "deploy",
        "release_name": RELEASE,
        "target_namespace": NAMESPACE,
        "chart_path": str(CHART),
        "image_repository": recorder.context["pullRepository"],
        "artifact_digest": digest,
        "kube_context": recorder.context["kubeContext"],
    }
    recorder.run(
        step,
        [
            str(PROJECT_ROOT / ".venv/bin/ansible-playbook"),
            "-i", str(PROJECT_ROOT / "deploy/ansible/inventories/localhost.ini"),
            str(PROJECT_ROOT / "deploy/ansible/playbooks/deploy-kubernetes.yml"),
            "--extra-vars", json.dumps(extra_vars, sort_keys=True),
        ],
        env={"ANSIBLE_COLLECTIONS_PATH": str(COLLECTIONS), "ANSIBLE_HOST_KEY_CHECKING": "False"},
        detail={"extraVars": extra_vars},
        timeout=900,
    )


def running_image(recorder: EvidenceRecorder, step: str) -> str:
    workload = kubectl_json(
        recorder, step, "get", "deployment", f"{RELEASE}-sample-kubernetes-app", "-n", NAMESPACE
    )
    return workload["spec"]["template"]["spec"]["containers"][0]["image"]


def service_health(recorder: EvidenceRecorder, step: str) -> dict:
    """Reach the Service through the API server rather than from inside the cluster.

    A helper pod would need to pull an image, and a lab cluster often has no outbound
    internet. `kubectl port-forward` tunnels over the API server connection that is
    already working, so the check exercises the real Service and Pod readiness without
    depending on cluster egress.
    """

    port = recorder.context["forwardPort"]
    command = [
        "kubectl", "--context", recorder.context["kubeContext"], "port-forward",
        f"service/{RELEASE}-sample-kubernetes-app", f"{port}:8080", "-n", NAMESPACE,
    ]
    started = time.time()
    forward = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        payload: dict | None = None
        last: Exception | None = None
        for _ in range(30):
            if forward.poll() is not None:
                break
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=5) as response:
                    payload = json.loads(response.read() or b"{}")
                    break
            except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
                last = exc
                time.sleep(1)
    finally:
        forward.terminate()
        try:
            forward.wait(timeout=10)
        except subprocess.TimeoutExpired:
            forward.kill()

    recorder.record(
        step,
        {
            "command": command,
            "durationSeconds": round(time.time() - started, 3),
            "response": payload,
            "error": None if payload else str(last),
        },
        passed=payload is not None,
    )
    if payload is None:
        raise GateFailure(f"the service never answered through port-forward: {last}")
    return payload


def gate(recorder: EvidenceRecorder) -> None:
    require_tools(recorder, "docker", "kubectl", "helm", "syft", "trivy", "cosign")
    context = recorder.context
    client = NetciClient(context["apiUrl"], context["pipelineApiKey"])

    health = client.health()
    recorder.record("api-health", health)
    recorder.check_equal("the netCI API is healthy", health.get("status"), "ok")

    # Before anything is built: the application layers on this, so a stale base is a
    # vulnerability-gate failure attributed to the application.
    build_golden_base(recorder)

    recorder.run("helm-lint", ["helm", "lint", str(CHART)])
    nodes = kubectl_json(recorder, "cluster-nodes", "get", "nodes")
    recorder.check("the kind cluster is reachable", len(nodes.get("items", [])) >= 1)

    if WORK_DIR.exists():
        shutil.rmtree(WORK_DIR)
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    keys = generate_key(recorder)

    first = build_and_publish(recorder, "v1", keys)
    second = build_and_publish(recorder, "v2", keys)
    recorder.check(
        "the two releases are distinct artifacts",
        first["artifactDigest"] != second["artifactDigest"],
        detail={"v1": first["artifactDigest"], "v2": second["artifactDigest"]},
    )

    name = f"gate-k8s-{uuid.uuid4().hex[:6]}"
    application = client.create_application(
        {
            "name": name,
            "repositoryUrl": f"https://github.com/example/{name}",
            "pipelineTemplate": "kubernetes-ci-cd-v1",
            "runtime": "kubernetes",
        },
        idempotency_key=f"gate-app-{name}",
    )

    run = client.start_pipeline(
        application["id"], {"commitSha": "ba11111", "environment": "staging"}, idempotency_key=f"gate-run-{name}-1"
    )
    client.ci_result(run["id"], {"status": "running"})
    decision = client.publish_evidence(run["id"], first)
    recorder.record("v1:policy-decision", decision)
    recorder.check_equal("the v1 artifact satisfies the supply-chain policy", decision["decision"], "allow")
    ci = client.ci_result(run["id"], {"status": "succeeded", "artifactDigest": first["artifactDigest"]})
    deployment = ci["deployment"]
    recorder.check_equal(
        "a staging deployment does not need approval", deployment["status"], "deploying"
    )
    recorder.check_equal("the deployment targets the Kubernetes runtime", deployment["runtime"], "kubernetes")

    helm_deploy(recorder, str(first["artifactDigest"]), step="deploy:v1")
    image = running_image(recorder, "v1:running-image")
    recorder.check_equal(
        "the pod runs exactly the digest netCI approved",
        image,
        f"{context['pullRepository']}@{first['artifactDigest']}",
    )
    healthy = service_health(recorder, "v1:in-cluster-health")
    recorder.record("v1:service-health", healthy)
    recorder.check_equal("the service reports healthy inside the cluster", healthy.get("status"), "ok")
    client.deployment_result(deployment["id"], "healthy", f"running {image}")

    # ----------------------------------------------------------- promote and revert
    second_run = client.start_pipeline(
        application["id"], {"commitSha": "ba22222", "environment": "staging"}, idempotency_key=f"gate-run-{name}-2"
    )
    client.ci_result(second_run["id"], {"status": "running"})
    client.publish_evidence(second_run["id"], second)
    second_ci = client.ci_result(
        second_run["id"], {"status": "succeeded", "artifactDigest": second["artifactDigest"]}
    )
    second_deployment = second_ci["deployment"]
    helm_deploy(recorder, str(second["artifactDigest"]), step="deploy:v2")
    recorder.check_equal(
        "the promoted release runs the second digest",
        running_image(recorder, "v2:running-image"),
        f"{context['pullRepository']}@{second['artifactDigest']}",
    )
    client.deployment_result(second_deployment["id"], "healthy", "v2 healthy")

    history = recorder.run(
        "helm-history",
        ["helm", "--kube-context", context["kubeContext"], "history", RELEASE, "-n", NAMESPACE, "-o", "json"],
    )
    revisions = json.loads(history.stdout or "[]")
    recorder.check(
        "Helm kept a revision history to roll back to",
        len(revisions) >= 2,
        detail=[item.get("revision") for item in revisions],
    )

    rolled_back = client.rollback(second_deployment["id"], str(first["artifactDigest"]), "kubernetes rollback drill")
    recorder.check(
        "netCI records the rollback",
        rolled_back["status"] in ("rolled_back", "rollback_in_progress"),
        detail=rolled_back["status"],
    )
    recorder.run(
        "helm-rollback",
        [
            "helm", "--kube-context", context["kubeContext"], "rollback", RELEASE,
            str(revisions[-2]["revision"]), "-n", NAMESPACE, "--wait", "--timeout", "180s",
        ],
        timeout=400,
    )
    recorder.check_equal(
        "rolling back the Helm revision restores the previous digest without rebuilding",
        running_image(recorder, "rollback:running-image"),
        f"{context['pullRepository']}@{first['artifactDigest']}",
    )
    restored = service_health(recorder, "rollback:in-cluster-health")
    recorder.check_equal("the rolled-back service is healthy", restored.get("status"), "ok")


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
    parser.add_argument(
        "--registry",
        default=os.getenv("NETCI_KIND_REGISTRY", "netci-registry:5000"),
        help="registry address as the cluster resolves it",
    )
    parser.add_argument(
        "--push-registry",
        default=os.getenv("NETCI_KIND_PUSH_REGISTRY", ""),
        help="registry address this host can reach (defaults to --registry)",
    )
    parser.add_argument("--kube-context", default=os.getenv("NETCI_KUBE_CONTEXT", "kind-netci-local"))
    parser.add_argument(
        "--forward-port",
        type=int,
        default=int(os.getenv("NETCI_LAB_K8S_FORWARD_PORT", "18083")),
        help="local port used to reach the Service through kubectl port-forward",
    )
    arguments = parser.parse_args()

    recorder = EvidenceRecorder(
        "e2e-kubernetes",
        description="Signed image promoted onto kind via Helm, with the running pod's digest "
        "compared to the approved artifact and a revision rollback verified.",
    )
    recorder.context.update(
        {
            "apiUrl": arguments.api_url,
            "pipelineApiKey": arguments.pipeline_api_key,
            "registry": arguments.registry,
            "pullRepository": f"{arguments.registry}/hello-kubernetes",
            "pushRepository": f"{arguments.push_registry or arguments.registry}/hello-kubernetes",
            "kubeContext": arguments.kube_context,
            "forwardPort": arguments.forward_port,
        }
    )
    return run_gate(recorder, gate)


if __name__ == "__main__":
    raise SystemExit(main())
