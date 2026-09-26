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
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
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
    # The token netCI minted for this run when it dispatched the build. It can report
    # for this run and nothing else. The shared key is a fallback only local mode accepts.
    token = setting("NETCI_CALLBACK_TOKEN") or setting("NETCI_PIPELINE_API_KEY")
    if not token:
        fail("NETCI_CALLBACK_TOKEN (per-run) or NETCI_PIPELINE_API_KEY (legacy, local only) is required")
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
    """Count *distinct* CVEs per severity from a Trivy JSON report.

    Not an exit code, and not raw occurrences. Trivy reports the same CVE once per
    affected package and once per target, so a single flaw routinely appears several
    times; counting occurrences would say "3 highs" where there is one thing to fix.

    Distinct identifiers is also the unit an exception is written in -- a waiver names a
    CVE -- so counting this way is what keeps the counts and `trivy_findings` in agreement.
    netCI refuses evidence where they disagree, precisely so this cannot drift.
    """

    seen: dict[str, set[str]] = {"critical": set(), "high": set(), "medium": set()}
    for result in report.get("Results", []) or []:
        if not isinstance(result, dict):
            continue
        for vulnerability in result.get("Vulnerabilities", []) or []:
            if not isinstance(vulnerability, dict):
                continue
            severity = str(vulnerability.get("Severity", "")).lower()
            if severity not in seen:
                continue
            identifier = str(vulnerability.get("VulnerabilityID", "")).strip()
            # An entry with no identifier still counts; it just cannot be de-duplicated
            # or waived, which is the honest outcome.
            seen[severity].add(identifier or f"__unidentified__{len(seen[severity])}")
    return {severity: len(identifiers) for severity, identifiers in seen.items()}


def trivy_findings(report: dict[str, object]) -> list[dict[str, str]]:
    """The blocking findings, by identifier.

    netCI's exception register waives a *named* CVE on a named digest, so evidence that
    reports only counts cannot be waived at all -- there is nothing to match against, and
    "three highs" is not something anyone can take responsibility for. Publishing the
    identifiers is what makes a governed exception possible; it also puts the package and
    fixed version in the audit trail, which is what someone triaging the finding needs.

    Only HIGH and CRITICAL are listed, because those are the severities that block.
    """

    findings: list[dict[str, str]] = []
    seen: set[str] = set()
    for result in report.get("Results", []) or []:
        if not isinstance(result, dict):
            continue
        for vulnerability in result.get("Vulnerabilities", []) or []:
            if not isinstance(vulnerability, dict):
                continue
            if str(vulnerability.get("Severity", "")).upper() not in {"HIGH", "CRITICAL"}:
                continue
            identifier = str(vulnerability.get("VulnerabilityID", "")).strip()
            if not identifier or identifier in seen:
                continue
            seen.add(identifier)
            findings.append(
                {
                    "id": identifier,
                    "severity": str(vulnerability.get("Severity", "")).upper(),
                    "package": str(vulnerability.get("PkgName", "")),
                    "installedVersion": str(vulnerability.get("InstalledVersion", "")),
                    "fixedVersion": str(vulnerability.get("FixedVersion", "")),
                }
            )
    return findings


def command_status(arguments: argparse.Namespace) -> int:
    payload: dict[str, object] = {"status": arguments.status, "logLines": list(arguments.log or [])}
    if arguments.status == "succeeded" and os.environ.get("NETCI_PUBLISH", "").strip() == "false":
        # A verify-only build published nothing, and netCI refuses a digest for it.
        pass
    elif arguments.status == "succeeded":
        digest = arguments.digest or read_text_file("artifact-digest.txt")
        if not digest.startswith(DIGEST_PREFIX):
            fail(f"a successful build must report a {DIGEST_PREFIX} digest, got {digest!r}")
        payload["artifactDigest"] = digest
    response = post(f"/pipeline-runs/{run_id()}/ci-result", payload)
    print(json.dumps(response.get("pipelineRun", response), indent=2))
    return 0


def command_stage(arguments: argparse.Namespace) -> int:
    """One stage's transition. The Portal draws the stage graph from these; without
    them a succeeded run showed nine "Pending" boxes forever."""

    payload: dict[str, object] = {
        "stageId": arguments.id,
        "stageName": arguments.name or arguments.id,
        "status": arguments.status,
        "attempt": int(setting("NETCI_STAGE_ATTEMPT", "1") or 1),
    }
    if arguments.started_at:
        payload["startedAt"] = arguments.started_at
    if arguments.completed_at:
        payload["completedAt"] = arguments.completed_at
    if arguments.duration_ms is not None:
        payload["durationMs"] = max(0, int(arguments.duration_ms))
    if arguments.error:
        payload["errorMessage"] = arguments.error[:2000]
    response = post(f"/pipeline-runs/{run_id()}/stages", payload)
    print(json.dumps(response, indent=2))
    return 0


def run_version_command(cmd: list[str], timeout: float = 3.0) -> str | None:
    """Run a tool version command with a strict timeout, returning trimmed stdout on success."""
    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            check=False,
        )
        if proc.returncode == 0:
            return proc.stdout.strip()
        return None
    except Exception:
        return None


def collect_tool_versions() -> dict[str, str | None]:
    """Collect installed tool versions from the build agent (ADR-056).

    Executes version inspection commands for syft, trivy, cosign, and buildah.
    Each command is best effort with a short timeout. If any tool is missing or fails,
    its value is recorded as None, ensuring build operations are never blocked.
    """
    versions: dict[str, str | None] = {
        "syft": None,
        "trivy": None,
        "cosign": None,
        "buildah": None,
        "trivyDbUpdatedAt": None,
        # When the build looked: netCI judges the Trivy DB's age against this, not "now".
        "reportedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }

    try:
        # syft version -o json
        syft_out = run_version_command(["syft", "version", "-o", "json"])
        if syft_out:
            try:
                data = json.loads(syft_out)
                if isinstance(data, dict):
                    v = data.get("version")
                    if v and isinstance(v, str):
                        versions["syft"] = v.strip().lstrip("v")
            except Exception:
                pass

        # trivy --version --format json
        trivy_out = run_version_command(["trivy", "--version", "--format", "json"])
        if trivy_out:
            try:
                data = json.loads(trivy_out)
                if isinstance(data, dict):
                    v = data.get("Version") or data.get("version")
                    if v and isinstance(v, str):
                        versions["trivy"] = v.strip().lstrip("v")
                    vuln_db = data.get("VulnerabilityDB")
                    if isinstance(vuln_db, dict):
                        updated_at = vuln_db.get("UpdatedAt") or vuln_db.get("updatedAt")
                        if updated_at and isinstance(updated_at, str):
                            versions["trivyDbUpdatedAt"] = updated_at.strip()
            except Exception:
                pass

        # cosign version --json
        cosign_out = run_version_command(["cosign", "version", "--json"])
        if cosign_out:
            try:
                data = json.loads(cosign_out)
                if isinstance(data, dict):
                    v = data.get("GitVersion") or data.get("gitVersion") or data.get("version")
                    if v and isinstance(v, str):
                        versions["cosign"] = v.strip().lstrip("v")
            except Exception:
                pass

        # buildah --version
        buildah_out = run_version_command(["buildah", "--version"])
        if buildah_out:
            try:
                m = re.search(r"version\s+([0-9]+(?:\.[0-9]+)+)", buildah_out, re.IGNORECASE)
                if m:
                    versions["buildah"] = m.group(1)
                else:
                    m_fallback = re.search(r"([0-9]+\.[0-9]+(?:\.[0-9]+)?)", buildah_out)
                    if m_fallback:
                        versions["buildah"] = m_fallback.group(1)
            except Exception:
                pass
    except Exception:
        pass

    return versions


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
    findings = trivy_findings(scan_report)

    signature_path = output_dir() / arguments.signature_file
    signature_verified = signature_path.is_file() and signature_path.stat().st_size > 0

    # What the test stage recorded (test-result.json), so a version registered from
    # this run carries its automation evidence without a second, per-tag report.
    ci_report: dict[str, object] | None = None
    result_path = output_dir() / "test-result.json"
    if result_path.is_file():
        try:
            raw = json.loads(result_path.read_text(encoding="utf-8"))
            if raw.get("autoTest") in {"passed", "failed", "skipped"}:
                ci_report = {"autoTest": raw["autoTest"], "runner": str(raw.get("runner") or "")[:64]}
                if isinstance(raw.get("coverage"), (int, float)):
                    ci_report["coverage"] = float(raw["coverage"])
                if isinstance(raw.get("testsRun"), int):
                    ci_report["testsRun"] = raw["testsRun"]
        except (OSError, json.JSONDecodeError):
            ci_report = None

    payload = {
        "artifactDigest": digest,
        "artifactRef": read_text_file("artifact-ref.txt", required=False) or None,
        "buildRunId": setting("BUILD_TAG") or None,
        "ciReport": ci_report,
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
            "findings": findings,
            "reportLocation": arguments.scan_location or str(scan_path),
        },
        "signature": {
            "provider": "cosign",
            "verified": signature_verified,
            "certificateIdentity": setting("COSIGN_IDENTITY") or None,
            "bundleLocation": str(signature_path) if signature_verified else None,
        },
        "toolVersions": collect_tool_versions(),
    }
    provenance = provenance_evidence()
    if provenance is not None:
        payload["provenance"] = provenance
    response = post(f"/pipeline-runs/{run_id()}/security-evidence", payload)
    print(json.dumps(response, indent=2))
    if response.get("decision") != "allow":
        fail(f"netCI supply-chain policy denied this artifact: {response.get('reason')}")

    # The SBOM itself, after the evidence: netCI records it against the digest that
    # evidence names, so it keeps what the artifact contains after this agent is gone
    # and can rescan it for vulnerabilities published later (ADR-045).
    try:
        document = json.loads(sbom_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        fail("sbom.json is not JSON")
    sbom_response = post(f"/pipeline-runs/{run_id()}/sbom", document)
    print(f"SBOM recorded: {sbom_response.get('components', 0)} components")
    return 0


SLSA_PROVENANCE_V1 = "https://slsa.dev/provenance/v1"
BUILD_TYPE = "https://github.com/netci/netci-delivery-platform/buildtypes/jenkins-shared-library/v1"


def public_url(url: str) -> str:
    """A repository URL without credentials. Provenance is stored in the registry beside
    the image, where anyone who can pull can read it; a token in `https://user:tok@...`
    would be published with every build."""

    parts = urllib.parse.urlsplit(url)
    if parts.username is None and parts.password is None:
        return url
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    return urllib.parse.urlunsplit((parts.scheme, host, parts.path, parts.query, parts.fragment))


def provenance_predicate() -> dict[str, object]:
    """SLSA v1 provenance for this build, from what netCI told the build to do.

    Every field comes from the job parameters netCI set or from Jenkins itself -- the
    repository, the exact commit, the directory, the image name, the run -- so the worker
    can check at deploy time that the image was built from the commit netCI dispatched,
    without asking netCI's database.
    """

    repository = public_url(setting("GIT_URL"))
    commit = setting("COMMIT_SHA")
    if not repository or not commit:
        fail("provenance needs GIT_URL and COMMIT_SHA; this build was not dispatched by netCI")
    workspace = setting("WORKSPACE")
    app_dir = setting("NETCI_APP_DIR", ".")
    if workspace and os.path.isabs(app_dir):
        app_dir = os.path.relpath(app_dir, workspace)
    jenkins = setting("JENKINS_URL").rstrip("/")
    return {
        "buildDefinition": {
            "buildType": BUILD_TYPE,
            "externalParameters": {
                "repository": repository,
                "ref": setting("GIT_BRANCH"),
                "commit": commit,
                "appDir": app_dir,
                "imageName": setting("NETCI_IMAGE_NAME"),
                "pipelineRunId": setting("NETCI_PIPELINE_RUN_ID"),
            },
            "internalParameters": {
                "template": setting("NETCI_TEMPLATE"),
                "stages": setting("NETCI_STAGES"),
                "environment": setting("NETCI_ENVIRONMENT"),
            },
            "resolvedDependencies": [
                {"uri": f"git+{repository}", "digest": {"gitCommit": commit}},
            ],
        },
        "runDetails": {
            "builder": {"id": f"{jenkins or 'jenkins'}#netci-shared-library"},
            "metadata": {
                "invocationId": setting("BUILD_URL") or setting("BUILD_TAG"),
                "finishedOn": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            },
        },
    }


def command_provenance(arguments: argparse.Namespace) -> int:
    target = output_dir() / arguments.output
    target.write_text(json.dumps(provenance_predicate(), indent=2, sort_keys=True), encoding="utf-8")
    print(f"provenance predicate written to {target}")
    return 0


def provenance_evidence() -> dict[str, object] | None:
    """What the Sign stage attested and then verified; None when it attested nothing."""

    verified = output_dir() / "provenance.verify.json"
    predicate_path = output_dir() / "provenance.json"
    if not predicate_path.is_file():
        return None
    try:
        predicate = json.loads(predicate_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        fail("provenance.json is unreadable")
    external = predicate.get("buildDefinition", {}).get("externalParameters", {})
    return {
        "predicateType": SLSA_PROVENANCE_V1,
        "verified": verified.is_file() and verified.stat().st_size > 0,
        "repository": external.get("repository"),
        "commit": external.get("commit"),
        "builderId": predicate.get("runDetails", {}).get("builder", {}).get("id"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    subcommands = parser.add_subparsers(dest="command", required=True)

    status = subcommands.add_parser("status", help="report a pipeline run status transition")
    status.add_argument("--status", required=True, choices=["running", "succeeded", "failed"])
    status.add_argument("--digest", default="", help="artifact digest (default: read artifact-digest.txt)")
    status.add_argument("--log", action="append", help="a log line to attach; repeatable")
    status.set_defaults(handler=command_status)

    stage = subcommands.add_parser("stage", help="report one pipeline stage transition")
    stage.add_argument("--id", required=True, help="stage id, e.g. unit-test")
    stage.add_argument("--name", default="", help="human name, e.g. Unit Test")
    stage.add_argument("--status", required=True, choices=["queued", "running", "succeeded", "failed", "cancelled", "skipped"])
    stage.add_argument("--started-at", default="", help="ISO-8601")
    stage.add_argument("--completed-at", default="", help="ISO-8601")
    stage.add_argument("--duration-ms", type=int, default=None)
    stage.add_argument("--error", default="")
    stage.set_defaults(handler=command_stage)

    evidence = subcommands.add_parser("evidence", help="publish SBOM, scan and signature evidence")
    evidence.add_argument("--digest", default="")
    evidence.add_argument("--sbom-location", default="", help="durable URI of the stored SBOM")
    evidence.add_argument("--scan-location", default="", help="durable URI of the stored scan report")
    evidence.add_argument("--signature-file", default="signature.bundle.json")
    evidence.set_defaults(handler=command_evidence)

    provenance = subcommands.add_parser("provenance", help="write the SLSA v1 provenance predicate for this build")
    provenance.add_argument("--output", default="provenance.json")
    provenance.set_defaults(handler=command_provenance)

    arguments = parser.parse_args()
    return int(arguments.handler(arguments))


if __name__ == "__main__":
    raise SystemExit(main())
