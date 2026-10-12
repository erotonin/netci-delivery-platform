"""Unit tests for TrivySbomScanner, UnavailableSbomScanner, and scanner factory."""

from __future__ import annotations

import json
import subprocess
from unittest.mock import MagicMock

import pytest

from app.adapters.trivy_rescan import (
    ScannedFinding,
    SbomScanError,
    TrivySbomScanner,
    UnavailableSbomScanner,
    build_sbom_scanner,
)


def test_trivy_scan_parses_findings_correctly(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda exe: f"/usr/bin/{exe}")

    report = {
        "Results": [
            {
                "Target": "bom.cdx.json",
                "Vulnerabilities": [
                    {
                        "VulnerabilityID": "CVE-2023-1234",
                        "Severity": "high",
                        "PkgName": "libssl",
                        "InstalledVersion": "1.1.1",
                        "FixedVersion": "1.1.2",
                    },
                    {
                        "VulnerabilityID": "CVE-2023-5678",
                        "Severity": "CRITICAL",
                        "PkgName": "curl",
                        "InstalledVersion": "7.80.0",
                        "FixedVersion": "7.88.0",
                    },
                    {
                        # Missing/empty vulnerability ID should be skipped
                        "VulnerabilityID": "",
                        "Severity": "LOW",
                    },
                ],
            }
        ]
    }

    mock_run = MagicMock()
    mock_run.return_value = subprocess.CompletedProcess(
        args=["trivy"], returncode=0, stdout=json.dumps(report), stderr=""
    )
    monkeypatch.setattr("subprocess.run", mock_run)

    scanner = TrivySbomScanner()
    findings = scanner.scan({"bomFormat": "CycloneDX", "components": []})

    assert len(findings) == 2
    assert findings[0] == ScannedFinding(
        vulnerability_id="CVE-2023-1234",
        severity="HIGH",
        package="libssl",
        installed_version="1.1.1",
        fixed_version="1.1.2",
    )
    assert findings[1] == ScannedFinding(
        vulnerability_id="CVE-2023-5678",
        severity="CRITICAL",
        package="curl",
        installed_version="7.80.0",
        fixed_version="7.88.0",
    )


def test_trivy_not_installed_raises(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda exe: None)
    scanner = TrivySbomScanner()
    with pytest.raises(SbomScanError) as exc_info:
        scanner.scan({})
    assert "trivy is not installed on this host" in str(exc_info.value)


def test_trivy_nonzero_exit_raises(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda exe: f"/usr/bin/{exe}")
    mock_run = MagicMock()
    mock_run.return_value = subprocess.CompletedProcess(
        args=["trivy"],
        returncode=1,
        stdout="",
        stderr="database download failed: connection timeout",
    )
    monkeypatch.setattr("subprocess.run", mock_run)

    scanner = TrivySbomScanner()
    with pytest.raises(SbomScanError) as exc_info:
        scanner.scan({})
    assert "trivy failed: database download failed: connection timeout" in str(exc_info.value)


def test_trivy_timeout_raises(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda exe: f"/usr/bin/{exe}")
    mock_run = MagicMock(side_effect=subprocess.TimeoutExpired(cmd=["trivy"], timeout=300.0))
    monkeypatch.setattr("subprocess.run", mock_run)

    scanner = TrivySbomScanner(timeout_seconds=300.0)
    with pytest.raises(SbomScanError) as exc_info:
        scanner.scan({})
    assert "trivy did not finish within 300s" in str(exc_info.value)


def test_trivy_invalid_json_raises(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda exe: f"/usr/bin/{exe}")
    mock_run = MagicMock()
    mock_run.return_value = subprocess.CompletedProcess(
        args=["trivy"], returncode=0, stdout="plain text, not json", stderr=""
    )
    monkeypatch.setattr("subprocess.run", mock_run)

    scanner = TrivySbomScanner()
    with pytest.raises(SbomScanError) as exc_info:
        scanner.scan({})
    assert "trivy output is not JSON" in str(exc_info.value)


def test_unavailable_scanner_raises():
    scanner = UnavailableSbomScanner()
    assert scanner.name == "none"
    with pytest.raises(SbomScanError) as exc_info:
        scanner.scan({})
    assert "SBOM rescanning is not configured (NETCI_SBOM_RESCAN=none)" in str(exc_info.value)


def test_build_sbom_scanner(monkeypatch):
    # Default / empty -> UnavailableSbomScanner
    monkeypatch.delenv("NETCI_SBOM_RESCAN", raising=False)
    scanner = build_sbom_scanner()
    assert isinstance(scanner, UnavailableSbomScanner)

    monkeypatch.setenv("NETCI_SBOM_RESCAN", "none")
    scanner = build_sbom_scanner()
    assert isinstance(scanner, UnavailableSbomScanner)

    # trivy -> TrivySbomScanner
    monkeypatch.setenv("NETCI_SBOM_RESCAN", "trivy")
    monkeypatch.setenv("NETCI_TRIVY_EXECUTABLE", "/custom/trivy")
    monkeypatch.setenv("NETCI_TRIVY_DB_REPOSITORY", "ghcr.io/aquasecurity/trivy-db")
    monkeypatch.setenv("NETCI_TRIVY_CACHE_DIR", "/cache")
    monkeypatch.setenv("NETCI_SBOM_RESCAN_TIMEOUT", "45")
    scanner = build_sbom_scanner()
    assert isinstance(scanner, TrivySbomScanner)
    assert scanner.executable == "/custom/trivy"
    assert scanner.db_repository == "ghcr.io/aquasecurity/trivy-db"
    assert scanner.cache_dir == "/cache"
    assert scanner.timeout_seconds == 45.0

    # invalid mode -> ValueError naming NETCI_SBOM_RESCAN
    monkeypatch.setenv("NETCI_SBOM_RESCAN", "grype")
    with pytest.raises(ValueError) as exc_info:
        build_sbom_scanner()
    assert "NETCI_SBOM_RESCAN" in str(exc_info.value)
