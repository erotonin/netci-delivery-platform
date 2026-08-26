from __future__ import annotations

import argparse
import json
import platform
import re
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    detail: str


def command_path(name: str) -> str | None:
    return shutil.which(name)


def run(command: list[str], timeout: int = 15) -> tuple[bool, str]:
    executable = command_path(command[0])
    if executable is None:
        return False, f"command not found: {command[0]}"
    try:
        completed = subprocess.run(
            [executable, *command[1:]],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    output = (completed.stdout or completed.stderr).strip().splitlines()
    detail = output[0] if output else f"exit code {completed.returncode}"
    return completed.returncode == 0, detail


def version_tuple(output: str) -> tuple[int, ...]:
    match = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", output)
    if not match:
        return ()
    return tuple(int(value) for value in match.groups(default="0"))


def tool_check(name: str, version_args: list[str], minimum: tuple[int, ...] | None = None) -> CheckResult:
    ok, detail = run([name, *version_args])
    if ok and minimum is not None:
        actual = version_tuple(detail)
        if not actual or actual < minimum:
            return CheckResult(name, False, f"requires >= {'.'.join(map(str, minimum))}; found {detail}")
    return CheckResult(name, ok, detail)


def windows_checks() -> list[CheckResult]:
    checks = [
        CheckResult("operating-system", platform.system() == "Windows", platform.platform()),
        CheckResult("python", sys.version_info >= (3, 11), platform.python_version()),
        tool_check("git", ["--version"], (2, 40)),
        tool_check("node", ["--version"], (20, 19)),
        tool_check("npm", ["--version"], (10, 0)),
    ]
    required = [ROOT / ".git", ROOT / "frontend/package-lock.json", ROOT / "QUICKSTART.md"]
    checks.extend(CheckResult(str(path.relative_to(ROOT)), path.exists(), "present" if path.exists() else "missing") for path in required)
    return checks


def ubuntu_version() -> str | None:
    os_release = Path("/etc/os-release")
    if not os_release.exists():
        return None
    values: dict[str, str] = {}
    for line in os_release.read_text(encoding="utf-8").splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key] = value.strip().strip('"')
    return values.get("VERSION_ID") if values.get("ID") == "ubuntu" else None


def ubuntu_checks() -> list[CheckResult]:
    version = ubuntu_version()
    checks = [
        CheckResult("operating-system", platform.system() == "Linux", platform.platform()),
        CheckResult("ubuntu-version", version == "24.04", version or "not Ubuntu"),
        CheckResult("python", sys.version_info >= (3, 11), platform.python_version()),
        tool_check("git", ["--version"], (2, 40)),
        tool_check("node", ["--version"], (20, 19)),
        tool_check("npm", ["--version"], (10, 0)),
        tool_check("make", ["--version"]),
        tool_check("bash", ["--version"]),
        tool_check("docker", ["--version"]),
        tool_check("kubectl", ["version", "--client"]),
        tool_check("kind", ["version"]),
        tool_check("helm", ["version", "--short"]),
        tool_check("ansible-playbook", ["--version"]),
        tool_check("go", ["version"]),
        tool_check("syft", ["version"]),
        tool_check("trivy", ["--version"]),
        tool_check("cosign", ["version"]),
        tool_check("virsh", ["--version"]),
    ]
    docker_ok, docker_detail = run(["docker", "info"], timeout=30)
    compose_ok, compose_detail = run(["docker", "compose", "version"])
    virsh_ok, virsh_detail = run(["virsh", "uri"])
    checks.extend(
        [
            CheckResult("docker-engine", docker_ok, docker_detail),
            CheckResult("docker-compose-plugin", compose_ok, compose_detail),
            CheckResult("libvirt-connection", virsh_ok, virsh_detail),
        ]
    )
    return checks


def main() -> int:
    parser = argparse.ArgumentParser(description="Strict netCI host prerequisite check")
    parser.add_argument("--profile", choices=("auto", "windows", "ubuntu"), default="auto")
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args()
    profile = args.profile
    if profile == "auto":
        profile = "windows" if platform.system() == "Windows" else "ubuntu"

    results = windows_checks() if profile == "windows" else ubuntu_checks()
    success = all(item.ok for item in results)
    if args.as_json:
        print(json.dumps({"profile": profile, "success": success, "checks": [asdict(item) for item in results]}, indent=2))
    else:
        for item in results:
            print(f"{'PASS' if item.ok else 'FAIL'} {item.name}: {item.detail}")
        print(f"doctor {profile}: {'passed' if success else 'failed'}")
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
