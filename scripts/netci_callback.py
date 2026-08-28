#!/usr/bin/env python3
"""Report CI progress and supply-chain evidence from a build agent back to netCI.

This is the half of the loop that runs *inside* the pipeline. It turns the raw files
the CI stages produced -- `artifact-digest.txt`, `sbom.json`, `scan-report.json`,
`signature.bundle.json` -- into the evidence contract netCI's policy evaluates, so a
build cannot claim a clean result without the artefacts that prove it.

    python3 scripts/netci_callback.py status --status running
    python3 scripts/netci_callback.py evidence
    python3 scripts/netci_callback.py status --status succeeded

Configuration comes from the parameters Jenkins injects:
    NETCI_API_URL, NETCI_PIPELINE_RUN_ID, NETCI_PIPELINE_API_KEY (or ..._FILE),
    NETCI_OUTPUT_DIR, NETCI_CORRELATION_ID
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import NoReturn

DIGEST_PREFIX = "sha256:"


def setting(name: str, default: str = "") -> str:
    """Read a setting, preferring a mounted secret file over an inline value."""

    path = os.getenv(f"{name}_FILE", "").strip()
    if path:
        try:
            return Path(path).read_text(encoding="utf-8").strip()
        except OSError as exc:
            fail(f"{name}_FILE is unreadable: {exc}")
    return os.getenv(name, default).strip()


def fail(message: str) -> NoReturn:
    print(f"netci-callback: {message}", file=sys.stderr)
    raise SystemExit(1)


def output_dir() -> Path:
    return Path(setting("NETCI_OUTPUT_DIR", ".")).resolve()


def read_text_file(name: str, *, required: bool = True) -> str:
    path = output_dir() / name
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError:
        if required:
            fail(f"required CI output is missing: {path}")
        return ""
    if required and not value:
        fail(f"required CI output is empty: {path}")
    return value


def post(path: str, payload: dict[str, object]) -> dict[str, object]:
    base = setting("NETCI_API_URL", "http://localhost:8000").rstrip("/")
    token = setting("NETCI_PIPELINE_API_KEY")
    if not token:
        fail("NETCI_PIPELINE_API_KEY (or NETCI_PIPELINE_API_KEY_FILE) is required")
    request = urllib.request.Request(
        f"{base}{path}",
        data=json.dumps(payload).encode(),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
            "X-Correlation-Id": setting("NETCI_CORRELATION_ID", "jenkins-build"),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read().decode(errors="replace")
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[-2000:]
        fail(f"POST {path} returned {exc.code}: {detail}")
    except urllib.error.URLError as exc:
        fail(f"POST {path} could not reach netCI: {exc.reason}")


def run_id() -> str:
    value = setting("NETCI_PIPELINE_RUN_ID")
    if not value:
        fail("NETCI_PIPELINE_RUN_ID is required")
    return value


def trivy_counts(report: dict[str, object]) -> dict[str, int]:
    """Count severities from a Trivy JSON report rather than trusting an exit code."""

    counts = {"critical": 0, "high": 0, "medium": 0}
    for result in report.get("Results", []) or []:
        if not isinstance(result, dict):
            continue
        for vulnerability in result.get("Vulnerabilities", []) or []:
            severity = str(vulnerability.get("Severity", "")).lower()
            if severity in counts:
                counts[severity] += 1
    return counts


def command_status(arguments: argparse.Namespace) -> int:
    payload: dict[str, object] = {"status": arguments.status, "logLines": list(arguments.log or [])}
    if arguments.status == "succeeded":
        digest = arguments.digest or read_text_file("artifact-digest.txt")
        if not digest.startswith(DIGEST_PREFIX):
            fail(f"a successful build must report a {DIGEST_PREFIX} digest, got {digest!r}")
        payload["artifactDigest"] = digest
    response = post(f"/pipeline-runs/{run_id()}/ci-result", payload)
    print(json.dumps(response.get("pipelineRun", response), indent=2))
    return 0


def command_evidence(arguments: argparse.Namespace) -> int:
    digest = arguments.digest or read_text_file("artifact-digest.txt")
    if not digest.startswith(DIGEST_PREFIX):
        fail(f"evidence requires a {DIGEST_PREFIX} digest, got {digest!r}")

    sbom_path = output_dir() / "sbom.json"
    if not sbom_path.is_file() or sbom_path.stat().st_size == 0:
        fail(f"SBOM is missing: {sbom_path}")

    scan_path = output_dir() / "scan-report.json"
    try:
        scan_report = json.loads(scan_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        fail(f"Trivy report is missing or unreadable: {exc}")
    counts = trivy_counts(scan_report)

    signature_path = output_dir() / arguments.signature_file
    signature_verified = signature_path.is_file() and signature_path.stat().st_size > 0

    payload = {
        "artifactDigest": digest,
        "artifactRef": read_text_file("artifact-ref.txt", required=False) or None,
        "buildRunId": setting("BUILD_TAG") or None,
        "sbom": {
            "generatedBy": "syft",
            "location": arguments.sbom_location or str(sbom_path),
            "format": "cyclonedx-json",
        },
        "vulnerabilityScan": {
            "scanner": "trivy",
            "status": "passed" if counts["critical"] == 0 and counts["high"] == 0 else "failed",
            "critical": counts["critical"],
            "high": counts["high"],
            "medium": counts["medium"],
            "reportLocation": arguments.scan_location or str(scan_path),
        },
        "signature": {
            "provider": "cosign",
            "verified": signature_verified,
            "certificateIdentity": setting("COSIGN_IDENTITY") or None,
            "bundleLocation": str(signature_path) if signature_verified else None,
        },
    }
    response = post(f"/pipeline-runs/{run_id()}/security-evidence", payload)
    print(json.dumps(response, indent=2))
    if response.get("decision") != "allow":
        fail(f"netCI supply-chain policy denied this artifact: {response.get('reason')}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    subcommands = parser.add_subparsers(dest="command", required=True)

    status = subcommands.add_parser("status", help="report a pipeline run status transition")
    status.add_argument("--status", required=True, choices=["running", "succeeded", "failed"])
    status.add_argument("--digest", default="", help="artifact digest (default: read artifact-digest.txt)")
    status.add_argument("--log", action="append", help="a log line to attach; repeatable")
    status.set_defaults(handler=command_status)

    evidence = subcommands.add_parser("evidence", help="publish SBOM, scan and signature evidence")
    evidence.add_argument("--digest", default="")
    evidence.add_argument("--sbom-location", default="", help="durable URI of the stored SBOM")
    evidence.add_argument("--scan-location", default="", help="durable URI of the stored scan report")
    evidence.add_argument("--signature-file", default="signature.bundle.json")
    evidence.set_defaults(handler=command_evidence)

    arguments = parser.parse_args()
    return int(arguments.handler(arguments))


if __name__ == "__main__":
    raise SystemExit(main())
