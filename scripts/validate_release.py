from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import yaml


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "release-checklist.yaml"
# `portable` is the subset that runs anywhere with only Python and Node: no Docker, no
# cluster, no lab. It is what netCI's own CI runs, and what a developer can run on any
# machine. `windows` and `ubuntu` add the host checks each of those needs.
ALLOWED_PROFILES = {"portable", "windows", "ubuntu"}
ALLOWED_STATES = {"ready", "blocked"}
FALSE_GREEN_MARKERS = (
    "NETCI_E2E_READY",
    "NETCI_SECURITY_READY",
    "failure-drill/sample.json",
    "configure shared and ephemeral commands before measuring",
)


def make_targets() -> set[str]:
    text = (ROOT / "Makefile").read_text(encoding="utf-8")
    return set(re.findall(r"^([A-Za-z0-9_.-]+):(?:\s|$)", text, flags=re.MULTILINE))


def load_manifest(path: Path = DEFAULT_MANIFEST) -> dict[str, Any]:
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("release checklist must be a mapping")
    return payload


def validate_manifest(manifest: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    checks = manifest.get("checks")
    if not isinstance(checks, list) or not checks:
        return ["release checklist must contain a non-empty checks list"]

    known_targets = make_targets()
    seen: set[str] = set()
    for index, check in enumerate(checks):
        label = f"check[{index}]"
        if not isinstance(check, dict):
            errors.append(f"{label}: must be a mapping")
            continue
        check_id = check.get("id")
        if not isinstance(check_id, str) or not check_id:
            errors.append(f"{label}: missing id")
            continue
        label = check_id
        if check_id in seen:
            errors.append(f"{label}: duplicate id")
        seen.add(check_id)

        profiles = check.get("profiles")
        if not isinstance(profiles, list) or not profiles or not set(profiles) <= ALLOWED_PROFILES:
            errors.append(f"{label}: profiles must be a non-empty subset of {sorted(ALLOWED_PROFILES)}")
        state = check.get("state")
        if state not in ALLOWED_STATES:
            errors.append(f"{label}: state must be one of {sorted(ALLOWED_STATES)}")
        if state == "blocked" and not check.get("reason"):
            errors.append(f"{label}: blocked check requires a reason")

        command = check.get("command")
        if not isinstance(command, list) or not command or not all(isinstance(part, str) and part for part in command):
            errors.append(f"{label}: command must be a non-empty string list")
            continue
        if any(marker.lower() in " ".join(command).lower() for marker in FALSE_GREEN_MARKERS):
            errors.append(f"{label}: command contains a false-green marker")
        if command[0] == "make" and len(command) >= 2 and command[1] not in known_targets:
            errors.append(f"{label}: missing Makefile target {command[1]}")
        for part in command[1:]:
            normalized = part.replace("\\", "/")
            if normalized.startswith(("scripts/", "tests/")) and normalized.endswith((".py", ".sh")):
                if not (ROOT / normalized).is_file():
                    errors.append(f"{label}: referenced script does not exist: {normalized}")

    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    for marker in FALSE_GREEN_MARKERS:
        if marker.lower() in makefile.lower():
            errors.append(f"Makefile contains false-green marker: {marker}")
    return errors


def command_for_host(command: list[str]) -> list[str]:
    result = list(command)
    if result[0] in {"python", "python3"}:
        result[0] = sys.executable
        return result
    executable = shutil.which(result[0])
    if executable:
        result[0] = executable
    return result


def execute(checks: list[dict[str, Any]]) -> int:
    blocked = [check for check in checks if check.get("state") == "blocked" and check.get("required", True)]
    if blocked:
        for check in blocked:
            print(f"BLOCKED {check['id']}: {check['reason']}", file=sys.stderr)
        print("release gate cannot run while required checks are blocked", file=sys.stderr)
        return 2

    for check in checks:
        if check.get("state") != "ready":
            continue
        command = command_for_host(check["command"])
        print(f"RUN {check['id']}: {' '.join(check['command'])}")
        started = time.perf_counter()
        try:
            completed = subprocess.run(command, cwd=ROOT, check=False)
        except OSError as exc:
            print(f"FAIL {check['id']}: {exc}", file=sys.stderr)
            return 1
        duration = time.perf_counter() - started
        if completed.returncode != 0:
            print(f"FAIL {check['id']}: exit={completed.returncode} duration={duration:.2f}s", file=sys.stderr)
            return completed.returncode or 1
        print(f"PASS {check['id']}: {duration:.2f}s")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate or execute netCI release gates")
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--profile", choices=sorted(ALLOWED_PROFILES))
    parser.add_argument("--check", action="append", default=[])
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()

    try:
        manifest = load_manifest(args.manifest)
    except (OSError, yaml.YAMLError, ValueError) as exc:
        print(f"invalid release checklist: {exc}", file=sys.stderr)
        return 1
    errors = validate_manifest(manifest)
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1

    checks: list[dict[str, Any]] = manifest["checks"]
    if args.profile:
        checks = [check for check in checks if args.profile in check["profiles"]]
    if args.check:
        requested = set(args.check)
        known = {check["id"] for check in checks}
        missing = requested - known
        if missing:
            print(f"unknown checks for selected profile: {', '.join(sorted(missing))}", file=sys.stderr)
            return 1
        checks = [check for check in checks if check["id"] in requested]
    if args.execute and not args.profile:
        print("--execute requires --profile to prevent running the wrong host gate", file=sys.stderr)
        return 1

    if args.execute:
        return execute(checks)
    ready = sum(check.get("state") == "ready" for check in checks)
    blocked = sum(check.get("state") == "blocked" for check in checks)
    print(f"release checklist valid: {len(checks)} checks ({ready} ready, {blocked} blocked)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
