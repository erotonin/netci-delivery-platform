"""Seam for rescanning recorded CycloneDX SBOMs with Trivy (ADR-045).

When a build completes, CI uploads a CycloneDX SBOM. That records what packages
were in the artifact at build time. But new CVEs are published every day: a build
that was "clean" last month may be vulnerable today.

netCI stores SBOMs by immutable artifact digest, and rescans them itself with
Trivy against an up-to-date vulnerability database. This runs independently of
any build agent or deployment, answering "what vulnerabilities affect this artifact
right now?"

`NETCI_SBOM_RESCAN` selects the scanner:

    none    rescanning is disabled. Default because running a scanner requires
            trivy to be installed and has external network/database dependencies.
    trivy   run trivy sbom against the CycloneDX document. Fails closed: an SBOM
            that cannot be scanned produces an error, not an empty list of findings.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from typing import Protocol

logger = logging.getLogger(__name__)


class SbomScanError(RuntimeError):
    """Rescanning an SBOM failed or scanner is unavailable."""


@dataclass(frozen=True)
class ScannedFinding:
    vulnerability_id: str
    severity: str
    package: str
    installed_version: str
    fixed_version: str


class SbomScanner(Protocol):
    name: str

    def scan(self, document: dict) -> list[ScannedFinding]: ...


class TrivySbomScanner:
    name = "trivy"

    def __init__(
        self,
        *,
        executable: str = "trivy",
        db_repository: str = "",
        cache_dir: str = "/tmp/netci-trivy-cache",
        timeout_seconds: float = 300.0,
        insecure_registry: bool = False,
    ) -> None:
        self.executable = executable
        self.db_repository = db_repository
        # A database mirror in a plain-HTTP registry (the lab's) needs what the build's
        # scan.sh passes for the same mirror.
        self.insecure_registry = insecure_registry
        self.cache_dir = cache_dir
        self.timeout_seconds = timeout_seconds

    def scan(self, document: dict) -> list[ScannedFinding]:
        if shutil.which(self.executable) is None:
            raise SbomScanError("trivy is not installed on this host; SBOMs cannot be rescanned")

        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".cdx.json", delete=False, encoding="utf-8"
        )
        try:
            tmp.write(json.dumps(document))
            tmp.flush()
            tmp.close()

            command = [
                self.executable,
                "sbom",
                "--format",
                "json",
                "--quiet",
                "--scanners",
                "vuln",
                "--cache-dir",
                self.cache_dir,
            ]
            if self.db_repository:
                command.extend(["--db-repository", self.db_repository])
            if self.insecure_registry:
                command.append("--insecure")
            command.append(tmp.name)

            try:
                proc = subprocess.run(
                    command,
                    capture_output=True,
                    text=True,
                    timeout=self.timeout_seconds,
                )
            except subprocess.TimeoutExpired as exc:
                raise SbomScanError(f"trivy did not finish within {self.timeout_seconds:.0f}s") from exc

            if proc.returncode != 0:
                output = (proc.stderr or proc.stdout or "")[-600:]
                raise SbomScanError(f"trivy failed: {output}")

            try:
                report = json.loads(proc.stdout)
            except (json.JSONDecodeError, ValueError) as exc:
                raise SbomScanError("trivy output is not JSON") from exc

            findings: list[ScannedFinding] = []
            for result in report.get("Results") or []:
                for v in result.get("Vulnerabilities") or []:
                    vid = str(v.get("VulnerabilityID", "")).strip()
                    if not vid:
                        continue
                    findings.append(
                        ScannedFinding(
                            vulnerability_id=vid,
                            severity=str(v.get("Severity", "UNKNOWN")).upper(),
                            package=str(v.get("PkgName", "")),
                            installed_version=str(v.get("InstalledVersion", "")),
                            fixed_version=str(v.get("FixedVersion", "")),
                        )
                    )
            return findings
        finally:
            try:
                os.unlink(tmp.name)
            except OSError:
                pass


class UnavailableSbomScanner:
    name = "none"

    def scan(self, document: dict) -> list[ScannedFinding]:
        raise SbomScanError("SBOM rescanning is not configured (NETCI_SBOM_RESCAN=none)")


def build_sbom_scanner() -> SbomScanner:
    mode = os.getenv("NETCI_SBOM_RESCAN", "none").strip().lower()
    if mode in {"", "none"}:
        return UnavailableSbomScanner()
    if mode == "trivy":
        return TrivySbomScanner(
            executable=os.getenv("NETCI_TRIVY_EXECUTABLE", "trivy") or "trivy",
            db_repository=os.getenv("NETCI_TRIVY_DB_REPOSITORY", "").strip(),
            cache_dir=os.getenv("NETCI_TRIVY_CACHE_DIR", "/tmp/netci-trivy-cache"),
            timeout_seconds=float(os.getenv("NETCI_SBOM_RESCAN_TIMEOUT", "300")),
            insecure_registry=os.getenv("NETCI_TRIVY_INSECURE", "false").strip().lower() in {"1", "true", "yes"},
        )
    raise ValueError(f"NETCI_SBOM_RESCAN must be none or trivy (got {mode!r})")
