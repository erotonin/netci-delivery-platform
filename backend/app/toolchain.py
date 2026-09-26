"""Central toolchain management and drift detection (ADR-056).

Tool versions, download URLs and checksums are pinned in toolchain/versions.yaml,
which acts as the single source of truth for CI build containers and security verifiers.
This module validates the pinned declarations, compares observed tool versions gathered
from CI build agents to detect version drift, and tracks the freshness of Trivy's
vulnerability database.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NamedTuple

import yaml


class ToolchainValidationError(ValueError):
    """Raised when toolchain/versions.yaml contains invalid or incomplete declarations."""
    pass


class TrivyDbAge(NamedTuple):
    """Calculated vulnerability database age and staleness verdict."""
    age_hours: float | None
    stale: bool


def find_versions_file() -> Path:
    """Locate toolchain/versions.yaml relative to repository root or container app root.

    In production containers the file is copied into /app/toolchain/versions.yaml;
    in local development it sits at <repo_root>/toolchain/versions.yaml.
    """
    env_override = os.getenv("NETCI_TOOLCHAIN_VERSIONS_PATH", "").strip()
    if env_override:
        path = Path(env_override)
        if path.is_file():
            return path
        raise FileNotFoundError(f"NETCI_TOOLCHAIN_VERSIONS_PATH does not exist: {env_override}")

    base = Path(__file__).resolve()
    candidates = [
        base.parents[2] / "toolchain" / "versions.yaml",  # <repo>/backend/app -> <repo>/toolchain
        base.parents[1] / "toolchain" / "versions.yaml",  # /app/app -> /app/toolchain
        Path.cwd() / "toolchain" / "versions.yaml",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate

    raise FileNotFoundError("toolchain/versions.yaml could not be found")


def validate_toolchain_dict(data: Any) -> dict[str, Any]:
    """Ensure declared versions map contains all required tools and integrity pins.

    A release cannot claim reproducible security scanning if tool versions or
    checksums are omitted. Buildah originates from apt, so it records source and package
    without a sha256 checksum.
    """
    if not isinstance(data, dict):
        raise ToolchainValidationError("toolchain specification must be a mapping")

    tools = data.get("tools")
    if not isinstance(tools, dict):
        raise ToolchainValidationError("toolchain specification missing 'tools' mapping")

    required_tools = {"syft", "trivy", "cosign", "buildah"}
    missing = required_tools - set(tools.keys())
    if missing:
        raise ToolchainValidationError(f"missing required tool declarations: {', '.join(sorted(missing))}")

    for name, spec in tools.items():
        if not isinstance(spec, dict):
            raise ToolchainValidationError(f"tool definition for '{name}' must be a mapping")
        if name == "buildah":
            if spec.get("source") != "apt":
                raise ToolchainValidationError("tool 'buildah' must specify source 'apt'")
        else:
            if not spec.get("version"):
                raise ToolchainValidationError(f"tool '{name}' requires a non-empty version")
            if not spec.get("url"):
                raise ToolchainValidationError(f"tool '{name}' requires a non-empty download url")
            if not spec.get("sha256"):
                raise ToolchainValidationError(f"tool '{name}' requires a non-empty sha256 checksum")

    toolbox = data.get("toolbox")
    if not isinstance(toolbox, dict):
        raise ToolchainValidationError("toolchain specification missing 'toolbox' mapping")
    image_name = toolbox.get("image") or toolbox.get("name")
    if not image_name or not toolbox.get("tag"):
        raise ToolchainValidationError("toolbox definition requires image name and tag")

    trivy_db = data.get("trivyDb")
    if not isinstance(trivy_db, dict):
        raise ToolchainValidationError("toolchain specification missing 'trivyDb' mapping")
    max_age = trivy_db.get("maxAgeHours")
    if not isinstance(max_age, (int, float)) or max_age <= 0:
        raise ToolchainValidationError("trivyDb.maxAgeHours must be a positive number")

    return data


def load_declared(path: Path | None = None) -> dict[str, Any]:
    """Load and validate toolchain/versions.yaml from disk."""
    target_path = path or find_versions_file()
    try:
        content = target_path.read_text(encoding="utf-8")
        raw = yaml.safe_load(content)
    except (OSError, yaml.YAMLError) as exc:
        raise ToolchainValidationError(f"failed to read toolchain configuration: {exc}") from exc

    return validate_toolchain_dict(raw)


def declared() -> dict[str, Any]:
    """Return the authoritative declared toolchain specifications."""
    return load_declared()


def _normalize_version(version: Any) -> str | None:
    """Normalize version string for comparison, stripping leading 'v' prefix."""
    if version is None:
        return None
    val = str(version).strip()
    if val.startswith("v") and len(val) > 1 and val[1].isdigit():
        return val[1:]
    return val


def compare(
    observed: dict[str, Any] | None,
    declared_map: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Compare observed tool versions against declared versions to detect drift.

    Returns a list of drift entries: {'tool': str, 'declared': str, 'observed': str}.
    Tools with source 'apt' (e.g. buildah) that have no pinned version do not flag drift.
    """
    if not observed or not isinstance(observed, dict):
        return []

    spec = declared_map if declared_map is not None else declared()
    declared_tools: dict[str, Any] = spec.get("tools", {})
    drifts: list[dict[str, Any]] = []

    for tool_name, tool_spec in declared_tools.items():
        declared_ver = tool_spec.get("version")
        if not declared_ver:
            continue

        if tool_name not in observed:
            continue

        observed_ver = observed.get(tool_name)
        if _normalize_version(observed_ver) != _normalize_version(declared_ver):
            drifts.append({
                "tool": tool_name,
                "declared": declared_ver,
                "observed": observed_ver,
            })

    return drifts


def trivy_db_age(
    updated_at: str | datetime | None,
    now: datetime | None = None,
    max_age_hours: float | None = None,
) -> TrivyDbAge:
    """Compute the age of Trivy's vulnerability database and return staleness flag.

    If updated_at is missing or unparseable, the database is considered stale (fail-closed).
    """
    if max_age_hours is None:
        spec = declared()
        max_age_hours = float(spec.get("trivyDb", {}).get("maxAgeHours", 72))

    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    if not updated_at:
        return TrivyDbAge(age_hours=None, stale=True)

    dt: datetime
    if isinstance(updated_at, datetime):
        dt = updated_at
    else:
        raw_str = str(updated_at).strip().replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(raw_str)
        except ValueError:
            return TrivyDbAge(age_hours=None, stale=True)

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    elapsed_seconds = (now - dt).total_seconds()
    age_hours = round(max(0.0, elapsed_seconds / 3600.0), 2)
    stale = age_hours > max_age_hours

    return TrivyDbAge(age_hours=age_hours, stale=stale)
