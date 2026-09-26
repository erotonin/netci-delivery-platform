"""Tests for central toolchain management (ADR-056).

Covers:
1. Toolchain configuration loading and validation (toolchain/versions.yaml).
2. Drift detection (compare observed agent versions against declared spec).
3. Trivy vulnerability database freshness calculation and staleness alerting.
4. Tool version collection in scripts/netci_callback.py (stub commands, missing tools, no build failure).
5. GET /toolchain API route behavior with declared spec, observed controllers, drift, and Trivy DB status.
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import app.main as main
import netci_callback
from app.domain.models import PipelineRun, PipelineStatus
from app.main import app
from app.toolchain import (
    ToolchainValidationError,
    compare,
    declared,
    load_declared,
    trivy_db_age,
    validate_toolchain_dict,
)

client = TestClient(app)


def setup_function():
    main.platform.reset()
    main.portal.reset()


# ------------------------------------------------------------------- file validation


def test_load_declared_from_repository_file():
    """Authoritative toolchain/versions.yaml must load and validate cleanly."""
    data = load_declared()
    assert isinstance(data, dict)
    assert "tools" in data
    assert "toolbox" in data
    assert "trivyDb" in data

    tools = data["tools"]
    for required in ("syft", "trivy", "cosign", "buildah", "go"):
        assert required in tools

    assert tools["syft"]["version"]
    assert tools["syft"]["sha256"]
    assert tools["trivy"]["version"]
    assert tools["trivy"]["sha256"]
    assert tools["cosign"]["version"]
    assert tools["cosign"]["sha256"]
    assert tools["buildah"]["source"] == "apt"

    toolbox = data["toolbox"]
    assert toolbox["image"]
    assert toolbox["tag"]

    assert data["trivyDb"]["maxAgeHours"] == 72


def test_load_declared_missing_file_raises_validation_error(tmp_path: Path):
    nonexistent = tmp_path / "does-not-exist.yaml"
    with pytest.raises(ToolchainValidationError):
        load_declared(nonexistent)


def test_validate_toolchain_dict_rejects_missing_required_tools():
    incomplete = {
        "toolbox": {"image": "netci/ci-toolbox", "tag": "0.4.0"},
        "tools": {
            "syft": {"version": "1.0.0", "url": "https://example.com", "sha256": "abc"},
            # missing trivy, cosign, buildah
        },
        "trivyDb": {"maxAgeHours": 72},
    }
    with pytest.raises(ToolchainValidationError) as exc:
        validate_toolchain_dict(incomplete)
    assert "missing required tool declarations" in str(exc.value)


def test_validate_toolchain_dict_rejects_buildah_without_apt_source():
    invalid_buildah = {
        "toolbox": {"image": "netci/ci-toolbox", "tag": "0.4.0"},
        "tools": {
            "syft": {"version": "1.0.0", "url": "https://example.com", "sha256": "abc"},
            "trivy": {"version": "1.0.0", "url": "https://example.com", "sha256": "abc"},
            "cosign": {"version": "1.0.0", "url": "https://example.com", "sha256": "abc"},
            "buildah": {"version": "1.0.0", "url": "https://example.com", "sha256": "abc"},
        },
        "trivyDb": {"maxAgeHours": 72},
    }
    with pytest.raises(ToolchainValidationError) as exc:
        validate_toolchain_dict(invalid_buildah)
    assert "source 'apt'" in str(exc.value)


def test_validate_toolchain_dict_rejects_missing_sha256_or_version():
    no_sha = {
        "toolbox": {"image": "netci/ci-toolbox", "tag": "0.4.0"},
        "tools": {
            "syft": {"version": "1.0.0", "url": "https://example.com"},  # missing sha256
            "trivy": {"version": "1.0.0", "url": "https://example.com", "sha256": "abc"},
            "cosign": {"version": "1.0.0", "url": "https://example.com", "sha256": "abc"},
            "buildah": {"source": "apt", "package": "buildah"},
        },
        "trivyDb": {"maxAgeHours": 72},
    }
    with pytest.raises(ToolchainValidationError) as exc:
        validate_toolchain_dict(no_sha)
    assert "sha256" in str(exc.value)


def test_validate_toolchain_dict_rejects_invalid_trivy_db():
    invalid_db = {
        "toolbox": {"image": "netci/ci-toolbox", "tag": "0.4.0"},
        "tools": {
            "syft": {"version": "1.0.0", "url": "https://example.com", "sha256": "abc"},
            "trivy": {"version": "1.0.0", "url": "https://example.com", "sha256": "abc"},
            "cosign": {"version": "1.0.0", "url": "https://example.com", "sha256": "abc"},
            "buildah": {"source": "apt", "package": "buildah"},
        },
        "trivyDb": {"maxAgeHours": -5},
    }
    with pytest.raises(ToolchainValidationError) as exc:
        validate_toolchain_dict(invalid_db)
    assert "positive number" in str(exc.value)


# ------------------------------------------------------------------- compare / drift


def test_compare_zero_drift_when_observed_matches_declared():
    spec = declared()
    tools = spec["tools"]
    observed = {
        "syft": tools["syft"]["version"],
        "trivy": tools["trivy"]["version"],
        "cosign": tools["cosign"]["version"],
        "go": tools.get("go", {}).get("version"),
        "buildah": "1.33.7",
    }
    drifts = compare(observed)
    assert drifts == []


def test_compare_normalizes_v_prefix():
    spec = declared()
    tools = spec["tools"]
    observed = {
        "syft": f"v{tools['syft']['version']}",
        "trivy": f"v{tools['trivy']['version']}",
        "cosign": f"v{tools['cosign']['version']}",
    }
    drifts = compare(observed)
    assert drifts == []


def test_compare_flags_drift_on_version_mismatch():
    spec = declared()
    syft_decl = spec["tools"]["syft"]["version"]
    observed = {
        "syft": "0.99.0",  # outdated / mismatched
        "trivy": spec["tools"]["trivy"]["version"],
        "cosign": spec["tools"]["cosign"]["version"],
    }
    drifts = compare(observed)
    assert len(drifts) == 1
    assert drifts[0]["tool"] == "syft"
    assert drifts[0]["declared"] == syft_decl
    assert drifts[0]["observed"] == "0.99.0"


def test_compare_ignores_apt_tools_without_declared_version():
    """Buildah has source 'apt' and no pinned version; any observed version should not flag drift."""
    observed = {
        "buildah": "9.9.9",
    }
    drifts = compare(observed)
    assert drifts == []


def test_compare_handles_empty_or_none():
    assert compare(None) == []
    assert compare({}) == []


# ------------------------------------------------------------------- trivy db age


def test_trivy_db_age_fresh():
    now = datetime(2026, 9, 26, 12, 0, 0, tzinfo=timezone.utc)
    updated_at = (now - timedelta(hours=10)).isoformat()
    result = trivy_db_age(updated_at, now=now, max_age_hours=72)
    assert result.age_hours == 10.0
    assert not result.stale


def test_trivy_db_age_stale():
    now = datetime(2026, 9, 26, 12, 0, 0, tzinfo=timezone.utc)
    updated_at = (now - timedelta(hours=73)).isoformat()
    result = trivy_db_age(updated_at, now=now, max_age_hours=72)
    assert result.age_hours == 73.0
    assert result.stale


def test_trivy_db_age_missing_or_invalid_fails_closed():
    # Absent timestamp must be treated as stale
    res_none = trivy_db_age(None)
    assert res_none.age_hours is None
    assert res_none.stale

    res_empty = trivy_db_age("")
    assert res_empty.age_hours is None
    assert res_empty.stale

    res_invalid = trivy_db_age("not-a-datetime-string")
    assert res_invalid.age_hours is None
    assert res_invalid.stale


def test_trivy_db_age_parses_z_suffix():
    now = datetime(2026, 9, 26, 12, 0, 0, tzinfo=timezone.utc)
    updated_at = "2026-09-26T10:00:00Z"
    result = trivy_db_age(updated_at, now=now, max_age_hours=72)
    assert result.age_hours == 2.0
    assert not result.stale


# ------------------------------------------------------------------- netci_callback collection


def test_run_version_command_missing_executable():
    """Missing tool returns None, never raises."""
    res = netci_callback.run_version_command(["nonexistent-tool-xyz-12345", "--version"])
    assert res is None


def test_run_version_command_timeout(monkeypatch):
    """Timeout during version check returns None, never fails."""
    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args[0], timeout=5)

    monkeypatch.setattr(subprocess, "run", fake_run)
    res = netci_callback.run_version_command(["echo", "hello"])
    assert res is None


def test_collect_tool_versions_with_stub_executables(tmp_path: Path, monkeypatch):
    """When tools exist on PATH, their version commands are parsed accurately."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()

    # Stub syft
    syft_bin = bin_dir / "syft"
    syft_bin.write_text('#!/bin/sh\necho \'{"version": "1.51.0"}\'\n', encoding="utf-8")
    syft_bin.chmod(syft_bin.stat().st_mode | stat.S_IEXEC)

    # Stub trivy
    trivy_bin = bin_dir / "trivy"
    trivy_bin.write_text(
        '#!/bin/sh\necho \'{"Version": "0.73.0", "VulnerabilityDB": {"UpdatedAt": "2026-09-26T04:00:00Z"}}\'\n',
        encoding="utf-8",
    )
    trivy_bin.chmod(trivy_bin.stat().st_mode | stat.S_IEXEC)

    # Stub cosign
    cosign_bin = bin_dir / "cosign"
    cosign_bin.write_text('#!/bin/sh\necho \'{"GitVersion": "v3.1.2"}\'\n', encoding="utf-8")
    cosign_bin.chmod(cosign_bin.stat().st_mode | stat.S_IEXEC)

    # Stub buildah
    buildah_bin = bin_dir / "buildah"
    buildah_bin.write_text(
        '#!/bin/sh\necho "buildah version 1.33.7 (image-spec 1.0.2, runtime-spec 1.0.2)"\n',
        encoding="utf-8",
    )
    buildah_bin.chmod(buildah_bin.stat().st_mode | stat.S_IEXEC)

    # Put stub bin_dir at front of PATH
    original_path = os.environ.get("PATH", "")
    monkeypatch.setenv("PATH", f"{bin_dir}:{original_path}")

    versions = netci_callback.collect_tool_versions()
    assert versions["syft"] == "1.51.0"
    assert versions["trivy"] == "0.73.0"
    assert versions["trivyDbUpdatedAt"] == "2026-09-26T04:00:00Z"
    assert versions["cosign"] == "3.1.2"
    assert versions["buildah"] == "1.33.7"


def test_collect_tool_versions_missing_tools_returns_null_and_does_not_fail(monkeypatch):
    """When tools are missing, each field is null and the function returns without error."""
    # Empty PATH: no tools available
    monkeypatch.setenv("PATH", "/nonexistent_bin_dir_empty")

    versions = netci_callback.collect_tool_versions()
    assert isinstance(versions, dict)
    assert versions["syft"] is None
    assert versions["trivy"] is None
    assert versions["cosign"] is None
    assert versions["buildah"] is None
    assert versions["trivyDbUpdatedAt"] is None


# ------------------------------------------------------------------- API route tests


def test_route_get_toolchain_empty_state():
    """GET /toolchain answers with declared spec, empty observed controllers and stale trivyDb."""
    res = client.get("/toolchain")
    assert res.status_code == 200
    data = res.json()

    assert "declared" in data
    assert "observed" in data
    assert "drift" in data
    assert "trivyDb" in data

    assert data["declared"]["toolbox"]["image"] == "netci/ci-toolbox"
    assert data["declared"]["toolbox"]["tag"] == "0.4.0"
    assert data["observed"] == []
    assert data["drift"] == []
    assert data["trivyDb"]["stale"] is True


def test_route_get_toolchain_with_observed_controllers():
    """GET /toolchain aggregates evidence per controller ID and detects version drift."""
    run_id_1 = uuid4()
    run_id_2 = uuid4()
    app_id = uuid4()

    evidence_1 = {
        "artifactDigest": "sha256:" + "a" * 64,
        "toolVersions": {
            "syft": "1.51.0",
            "trivy": "0.73.0",
            "cosign": "3.1.2",
            "buildah": "1.33.7",
            "trivyDbUpdatedAt": (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(),
        },
    }

    evidence_2 = {
        "artifactDigest": "sha256:" + "b" * 64,
        "toolVersions": {
            "syft": "1.49.0",  # drift!
            "trivy": "0.73.0",
            "cosign": "3.1.2",
            "buildah": "1.33.7",
            "trivyDbUpdatedAt": (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(),
        },
    }

    with main.database.transaction() as session:
        if hasattr(session, "_state"):
            now = datetime.now(timezone.utc)
            run1 = PipelineRun(
                id=run_id_1,
                application_id=app_id,
                commit_sha="1111111111111111111111111111111111111111",
                branch="main",
                environment=main.Environment.DEV,
                status=PipelineStatus.SUCCEEDED,
                created_at=now - timedelta(minutes=5),
                updated_at=now - timedelta(minutes=5),
                jenkins_run_id="jenkins-east:101",
            )
            run2 = PipelineRun(
                id=run_id_2,
                application_id=app_id,
                commit_sha="2222222222222222222222222222222222222222",
                branch="main",
                environment=main.Environment.DEV,
                status=PipelineStatus.SUCCEEDED,
                created_at=now - timedelta(minutes=1),
                updated_at=now - timedelta(minutes=1),
                jenkins_run_id="jenkins-west:202",
            )
            session._state.runs[run_id_1] = run1
            session._state.runs[run_id_2] = run2
            session._state.evidence[run_id_1] = evidence_1
            session._state.evidence[run_id_2] = evidence_2

    res = client.get("/toolchain")
    assert res.status_code == 200
    data = res.json()

    # Observed controllers should have jenkins-east and jenkins-west
    controllers = {item["controllerId"] for item in data["observed"]}
    assert "jenkins-east" in controllers
    assert "jenkins-west" in controllers

    # Drift should have detected syft 1.49.0 on jenkins-west
    drifts = data["drift"]
    assert any(d["tool"] == "syft" and d["observed"] == "1.49.0" for d in drifts)

    # Trivy DB freshness should be fresh (stale == False)
    assert data["trivyDb"]["stale"] is False
    assert data["trivyDb"]["observedUpdatedAt"] is not None
