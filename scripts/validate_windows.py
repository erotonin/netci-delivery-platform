from __future__ import annotations

import compileall
import pathlib
import sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
REQUIRED = [
    ROOT / "README.md",
    ROOT / "api/openapi.yaml",
    ROOT / "docker-compose.yml",
    ROOT / "jenkins/casc/base.yaml",
    ROOT / "templates/catalog.yaml",
    ROOT / "backstage/netci-template.yaml",
    ROOT / "deploy/ansible/playbooks/deploy-kubernetes.yml",
    ROOT / "deploy/ansible/playbooks/deploy-systemd.yml",
]

for path in REQUIRED:
    if not path.exists():
        raise SystemExit(f"missing required file: {path}")

for path in [ROOT / "api/openapi.yaml", ROOT / "docker-compose.yml", ROOT / "templates/catalog.yaml", ROOT / "backstage/netci-template.yaml"]:
    yaml.safe_load(path.read_text(encoding="utf-8"))

if not compileall.compile_dir(str(ROOT / "backend"), quiet=1):
    raise SystemExit("backend compile failed")

print(f"validation passed: {len(REQUIRED)} required files and backend syntax")
