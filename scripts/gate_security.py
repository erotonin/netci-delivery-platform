#!/usr/bin/env python3
"""Security gate: prove netCI allows a verified artifact and refuses an unverified one.

This runs the real supply chain -- Syft, Trivy and Cosign against a real artifact --
and then drives the netCI API, so the recorded decision is one the platform actually
made rather than a fixture.

Three cases, each asserted end to end:

  allow  a signed, scanned artifact with a clean fixable-vulnerability result is
         accepted and produces a deployment
  deny   an artifact whose Trivy scan found fixable HIGH/CRITICAL findings is refused,
         and no deployment is created
  deny   an artifact with no signature is refused even though its scan is clean

    python scripts/gate_security.py --api-url http://127.0.0.1:8000
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from netci_gates.client import NetciClient  # noqa: E402
from netci_gates.evidence import (  # noqa: E402
    PROJECT_ROOT,
    EvidenceRecorder,
    require_tools,
    run_gate,
)

WORK_DIR = PROJECT_ROOT / ".netci-gate" / "security"


def severity_counts(report: dict) -> dict[str, int]:
    counts = {"critical": 0, "high": 0, "medium": 0}
    for result in report.get("Results") or []:
        for vulnerability in result.get("Vulnerabilities") or []:
            severity = str(vulnerability.get("Severity", "")).lower()
            if severity in counts:
                counts[severity] += 1
    return counts


def build_signed_binary(recorder: EvidenceRecorder, work: Path) -> dict[str, object]:
    """Produce a genuinely verifiable artifact: build, SBOM, scan, sign a Go binary."""

    output = work / "clean"
    output.mkdir(parents=True, exist_ok=True)
    app_dir = PROJECT_ROOT / "sample-apps" / "hello-systemd-go"
    env = {"NETCI_OUTPUT_DIR": str(output), "VERSION": "v0.1.0", "NETCI_APP_DIR": str(app_dir)}
    ci = PROJECT_ROOT / "templates" / "systemd-ansible-ci-cd-v1" / "scripts" / "ci"

    recorder.run("clean:unit-test", ["bash", str(ci / "test.sh")], env=env)
    recorder.run("clean:build", ["bash", str(ci / "build.sh")], env=env)
    digest = (output / "artifact-digest.txt").read_text(encoding="utf-8").strip()
    recorder.check("the build produced an immutable sha256 digest", digest.startswith("sha256:"), detail=digest)

    recorder.run("clean:sbom", ["bash", str(ci / "sbom.sh")], env=env)
    sbom = json.loads((output / "sbom.json").read_text(encoding="utf-8"))
    recorder.check(
        "Syft produced a CycloneDX SBOM with components",
        sbom.get("bomFormat") == "CycloneDX" and bool(sbom.get("components") or sbom.get("metadata")),
        detail={"bomFormat": sbom.get("bomFormat"), "components": len(sbom.get("components") or [])},
    )

    # Trivy exits non-zero when it blocks, so a clean artifact must exit zero.
    recorder.run("clean:vulnerability-scan", ["bash", str(ci / "scan.sh")], env=env)
    scan = json.loads((output / "scan-report.json").read_text(encoding="utf-8"))
    counts = severity_counts(scan)
    recorder.check_equal("Trivy found no fixable critical vulnerabilities", counts["critical"], 0)
    recorder.check_equal("Trivy found no fixable high vulnerabilities", counts["high"], 0)

    key_ref, public_key = generate_cosign_key(recorder, work)
    recorder.run(
        "clean:sign",
        ["bash", str(ci / "sign.sh")],
        env={**env, "COSIGN_KEY_REF": key_ref, "COSIGN_PASSWORD": ""},
    )
    bundle = output / "signature.bundle.json"
    recorder.check("Cosign wrote a signature bundle", bundle.is_file() and bundle.stat().st_size > 0)

    recorder.run(
        "clean:verify-signature",
        [
            "cosign",
            "verify-blob",
            "--key",
            public_key,
            "--bundle",
            str(bundle),
            "--insecure-ignore-tlog=true",
            str(app_dir / "dist" / "hello-systemd-v0.1.0"),
        ],
        env={"COSIGN_PASSWORD": ""},
    )

    return {
        "artifactDigest": digest,
        "artifactRef": f"file://{app_dir / 'dist' / 'hello-systemd-v0.1.0'}",
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
            "bundleLocation": str(bundle),
        },
    }


def generate_cosign_key(recorder: EvidenceRecorder, work: Path) -> tuple[str, str]:
    """Generate a throwaway signing key. A real deployment injects one from a secret store."""

    key_dir = work / "keys"
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


def build_vulnerable_image(recorder: EvidenceRecorder, work: Path) -> dict[str, object]:
    """Build a container on a deliberately outdated base so Trivy has real findings."""

    output = work / "vulnerable"
    output.mkdir(parents=True, exist_ok=True)
    base = os.getenv("NETCI_GATE_VULNERABLE_BASE", "python:3.9-slim-buster")
    tag = "netci-gate-vulnerable:latest"
    # The deny case gets its own Dockerfile: the shipped sample deliberately patches its
    # base, and this case exists precisely to produce an unpatched one.
    context = output / "context"
    context.mkdir(parents=True, exist_ok=True)
    shutil.copy(PROJECT_ROOT / "sample-apps" / "hello-container" / "app.py", context / "app.py")
    (context / "Dockerfile").write_text(
        f"FROM {base}\n"
        "WORKDIR /app\n"
        "COPY app.py /app/app.py\n"
        "USER 10001:10001\n"
        'CMD ["python", "/app/app.py"]\n',
        encoding="utf-8",
    )

    build = recorder.run(
        "vulnerable:build",
        ["docker", "build", "--network", "host", "-t", tag, str(context)],
        detail={"baseImage": base, "why": "an unpatched end-of-life base image, so Trivy has real findings"},
    )
    recorder.check("the outdated base image built", build.returncode == 0)

    report = output / "scan-report.json"
    # Trivy blocks here, so a non-zero exit is the expected outcome.
    recorder.run(
        "vulnerable:vulnerability-scan",
        [
            "trivy", "image", "--severity", "HIGH,CRITICAL", "--ignore-unfixed",
            "--scanners", "vuln", "--exit-code", "1", "--format", "json",
            "--output", str(report), tag,
        ],
        expect_success=False,
    )
    counts = severity_counts(json.loads(report.read_text(encoding="utf-8")))
    recorder.check(
        "Trivy found fixable HIGH or CRITICAL vulnerabilities in the outdated image",
        counts["critical"] + counts["high"] > 0,
        detail=counts,
    )

    inspected = recorder.run("vulnerable:digest", ["docker", "image", "inspect", tag, "--format", "{{.Id}}"])
    digest = inspected.stdout.strip()
    recorder.check("the image has a sha256 identity", digest.startswith("sha256:"), detail=digest)

    return {
        "artifactDigest": digest,
        "artifactRef": tag,
        "sbom": {"generatedBy": "syft", "location": str(output / "sbom.json"), "format": "cyclonedx-json"},
        "vulnerabilityScan": {
            "scanner": "trivy",
            "status": "failed",
            "critical": counts["critical"],
            "high": counts["high"],
            "medium": counts["medium"],
            "reportLocation": str(report),
        },
        "signature": {"provider": "cosign", "verified": True, "bundleLocation": None},
    }


def provision_run(client: NetciClient, recorder: EvidenceRecorder, suffix: str) -> tuple[str, str]:
    """Create an application and get a pipeline run into the running state."""

    name = f"gate-sec-{suffix}-{uuid.uuid4().hex[:6]}"
    application = client.create_application(
        {
            "name": name,
            "repositoryUrl": f"https://github.com/example/{name}",
            "pipelineTemplate": "container-ci-cd-v1",
            "runtime": "docker",
        },
        idempotency_key=f"gate-app-{name}",
    )
    run = client.start_pipeline(
        application["id"],
        {"commitSha": "abc1234", "environment": "staging"},
        idempotency_key=f"gate-run-{name}",
    )
    client.ci_result(run["id"], {"status": "running"})
    recorder.record("provision-run", {"applicationId": application["id"], "pipelineRunId": run["id"], "name": name})
    return application["id"], run["id"]


def gate(recorder: EvidenceRecorder) -> None:
    require_tools(recorder, "syft", "trivy", "cosign", "docker", "go")
    api_url = recorder.context["apiUrl"]
    client = NetciClient(api_url, recorder.context["pipelineApiKey"])

    health = client.health()
    recorder.record("api-health", health)
    recorder.check_equal("the netCI API is reachable and healthy", health.get("status"), "ok")

    if WORK_DIR.exists():
        shutil.rmtree(WORK_DIR)
    WORK_DIR.mkdir(parents=True, exist_ok=True)

    # ---------------------------------------------------------------- allow case
    clean = build_signed_binary(recorder, WORK_DIR)
    application_id, run_id = provision_run(client, recorder, "allow")
    decision = client.publish_evidence(run_id, clean)
    recorder.record("allow:policy-decision", decision)
    recorder.check_equal("complete evidence is allowed by policy", decision["decision"], "allow")

    accepted = client.ci_result(run_id, {"status": "succeeded", "artifactDigest": clean["artifactDigest"]})
    recorder.check("a verified artifact produces a deployment", accepted.get("deployment") is not None)
    recorder.check_equal(
        "the deployment carries the digest that was scanned and signed",
        accepted["deployment"]["artifactDigest"],
        clean["artifactDigest"],
    )
    recorder.context["allowDeploymentId"] = accepted["deployment"]["id"]

    # ------------------------------------------------- deny case: vulnerabilities
    vulnerable = build_vulnerable_image(recorder, WORK_DIR)
    _, vulnerable_run = provision_run(client, recorder, "vuln")
    denied = client.publish_evidence(vulnerable_run, vulnerable)
    recorder.record("deny-vulnerable:policy-decision", denied)
    recorder.check_equal("a vulnerable artifact is denied by policy", denied["decision"], "deny")

    status, body = client.request(
        "POST",
        f"/pipeline-runs/{vulnerable_run}/ci-result",
        {"status": "succeeded", "artifactDigest": vulnerable["artifactDigest"]},
        machine=True,
    )
    recorder.record("deny-vulnerable:ci-result", {"status": status, "body": body})
    recorder.check_equal("CI success on a denied artifact is refused", status, 422)
    recorder.check_equal("the refusal names the policy", body.get("code"), "ARTIFACT_POLICY_DENIED")
    recorder.check_equal(
        "the denied run is failed, not left running",
        client.pipeline(vulnerable_run)["status"],
        "failed",
    )

    # ---------------------------------------------------- deny case: no signature
    unsigned = {**clean, "signature": {"provider": "cosign", "verified": False, "bundleLocation": None}}
    _, unsigned_run = provision_run(client, recorder, "unsigned")
    unsigned_decision = client.publish_evidence(unsigned_run, unsigned)
    recorder.record("deny-unsigned:policy-decision", unsigned_decision)
    recorder.check_equal("an unsigned artifact is denied even when its scan is clean", unsigned_decision["decision"], "deny")

    status, body = client.request(
        "POST",
        f"/pipeline-runs/{unsigned_run}/ci-result",
        {"status": "succeeded", "artifactDigest": unsigned["artifactDigest"]},
        machine=True,
    )
    recorder.check_equal("CI success on an unsigned artifact is refused", status, 422)

    # ------------------------------------------------------------- deny: mismatch
    _, mismatch_run = provision_run(client, recorder, "mismatch")
    client.publish_evidence(mismatch_run, clean)
    status, body = client.request(
        "POST",
        f"/pipeline-runs/{mismatch_run}/ci-result",
        {"status": "succeeded", "artifactDigest": "sha256:" + "9" * 64},
        machine=True,
    )
    recorder.record("deny-mismatch:ci-result", {"status": status, "body": body})
    recorder.check_equal("evidence for another digest does not authorise this artifact", status, 422)

    # ------------------------------------------------------------- audit surface
    logs = client.pipeline_logs(run_id)["lines"]
    recorder.record("allow:pipeline-log", {"lines": logs})
    recorder.check(
        "the policy decision is written to the run log",
        any(line.startswith("policy=allow") for line in logs),
        detail=logs,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--api-url", default=os.getenv("NETCI_API_URL", "http://127.0.0.1:8000"))
    parser.add_argument(
        "--pipeline-api-key",
        default=os.getenv("NETCI_PIPELINE_API_KEY", "netci-local-pipeline-key"),
    )
    arguments = parser.parse_args()

    recorder = EvidenceRecorder(
        "security-gate",
        description="Real Syft/Trivy/Cosign supply chain against the netCI policy: one allow and three deny cases.",
    )
    recorder.context.update({"apiUrl": arguments.api_url, "pipelineApiKey": arguments.pipeline_api_key})
    return run_gate(recorder, gate)


if __name__ == "__main__":
    raise SystemExit(main())
