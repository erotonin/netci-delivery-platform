"""Contract test ensuring toolchain/versions.yaml and the agent toolbox Dockerfile stay in sync.

ADR-056 establishes toolchain/versions.yaml as the central declarative authority
for pinned security and build tools. The toolbox Dockerfile builds the container
image that runs in Jenkins. If someone updates a tool version or checksum in the
Dockerfile without updating versions.yaml (or vice versa), the platform would
falsely report drift or fail to detect supply chain alterations.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE_PATH = ROOT / "jenkins/agent-toolbox/Dockerfile"
VERSIONS_YAML_PATH = ROOT / "toolchain/versions.yaml"


def parse_dockerfile_args(content: str) -> dict[str, str]:
    """Extract ARG key=value declarations from a Dockerfile."""
    pattern = re.compile(r"^ARG\s+([A-Za-z0-9_]+)=([^\s]+)", re.MULTILINE)
    args: dict[str, str] = {}
    for match in pattern.finditer(content):
        key, value = match.groups()
        args[key] = value.strip('"\'')
    return args


def load_versions_yaml() -> dict[str, Any]:
    """Load the declared toolchain configuration."""
    content = VERSIONS_YAML_PATH.read_text(encoding="utf-8")
    return yaml.safe_load(content)


def test_agent_toolbox_dockerfile_args_match_declared_versions():
    """Assert every version and sha256 checksum in Dockerfile matches toolchain/versions.yaml."""
    dockerfile_content = DOCKERFILE_PATH.read_text(encoding="utf-8")
    docker_args = parse_dockerfile_args(dockerfile_content)
    declared = load_versions_yaml()
    tools = declared.get("tools", {})

    # 1. syft
    assert "syft" in tools, "syft must be declared in toolchain/versions.yaml"
    syft = tools["syft"]
    assert syft["version"] == docker_args["SYFT_VERSION"]
    assert syft["sha256"] == docker_args["SYFT_SHA256_AMD64"]
    if "sha256_amd64" in syft:
        assert syft["sha256_amd64"] == docker_args["SYFT_SHA256_AMD64"]
    if "sha256_arm64" in syft:
        assert syft["sha256_arm64"] == docker_args["SYFT_SHA256_ARM64"]

    # 2. trivy
    assert "trivy" in tools, "trivy must be declared in toolchain/versions.yaml"
    trivy = tools["trivy"]
    assert trivy["version"] == docker_args["TRIVY_VERSION"]
    assert trivy["sha256"] == docker_args["TRIVY_SHA256_AMD64"]
    if "sha256_amd64" in trivy:
        assert trivy["sha256_amd64"] == docker_args["TRIVY_SHA256_AMD64"]
    if "sha256_arm64" in trivy:
        assert trivy["sha256_arm64"] == docker_args["TRIVY_SHA256_ARM64"]

    # 3. cosign
    assert "cosign" in tools, "cosign must be declared in toolchain/versions.yaml"
    cosign = tools["cosign"]
    assert cosign["version"] == docker_args["COSIGN_VERSION"]
    assert cosign["sha256"] == docker_args["COSIGN_SHA256_AMD64"]
    if "sha256_amd64" in cosign:
        assert cosign["sha256_amd64"] == docker_args["COSIGN_SHA256_AMD64"]
    if "sha256_arm64" in cosign:
        assert cosign["sha256_arm64"] == docker_args["COSIGN_SHA256_ARM64"]

    # 4. go
    assert "go" in tools, "go must be declared in toolchain/versions.yaml"
    go_tool = tools["go"]
    assert go_tool["version"] == docker_args["GO_VERSION"]
    assert go_tool["sha256"] == docker_args["GO_SHA256_AMD64"]
    if "sha256_amd64" in go_tool:
        assert go_tool["sha256_amd64"] == docker_args["GO_SHA256_AMD64"]
    if "sha256_arm64" in go_tool:
        assert go_tool["sha256_arm64"] == docker_args["GO_SHA256_ARM64"]


def test_buildah_is_declared_as_apt_package():
    """Buildah is installed from apt in the agent toolbox, not downloaded directly."""
    dockerfile_content = DOCKERFILE_PATH.read_text(encoding="utf-8")
    assert "buildah" in dockerfile_content, "Dockerfile must install buildah via apt"

    declared = load_versions_yaml()
    tools = declared.get("tools", {})
    assert "buildah" in tools
    buildah = tools["buildah"]
    assert buildah.get("source") == "apt"
    assert buildah.get("package") == "buildah"
    assert "sha256" not in buildah


def test_download_urls_match_declared_versions():
    """Assert tool download URLs reference the declared version tag."""
    declared = load_versions_yaml()
    tools = declared.get("tools", {})

    for name in ("syft", "trivy", "cosign", "go"):
        tool = tools[name]
        version = tool["version"]
        url = tool["url"]
        assert version in url, f"URL for {name} ({url}) must contain version {version}"


def test_the_controller_image_is_built_from_the_declared_plugins_and_base():
    """jenkins/plugins.txt and the Dockerfile's base are generated from versions.yaml (ADR-059)."""
    import subprocess
    import sys

    result = subprocess.run([sys.executable, str(ROOT / "scripts/toolchain_sync.py"), "--check"],
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr


def test_the_corp_controller_runs_the_declared_tag():
    declared = load_versions_yaml()["jenkins"]["controller"]
    values = yaml.safe_load((ROOT / "infra/corp/jenkins/values.yaml").read_text(encoding="utf-8"))
    image = values["controller"]["image"]
    assert image["repository"] == declared["image"] and image["tag"] == declared["tag"]
