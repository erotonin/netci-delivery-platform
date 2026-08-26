from __future__ import annotations

import compileall
import json
import pathlib
import re

import yaml

from validate_release import load_manifest, validate_manifest


ROOT = pathlib.Path(__file__).resolve().parents[1]
CORE_DOCS = [
    "docs/architecture.md",
    "docs/domain-model.md",
    "docs/api-contract.md",
    "docs/state-machine.md",
    "docs/security-model.md",
    "docs/dora-metrics.md",
    "docs/troubleshooting.md",
]
ADRS = [f"docs/decisions/ADR-{index:03d}-{name}.md" for index, name in enumerate(
    (
        "system-boundary",
        "custom-portal-backstage",
        "temporal-boundary",
        "jenkins-ci-netci-cd",
        "runtime-adapters",
        "jcasc-config-source-of-truth",
        "ephemeral-agent-isolation",
        "artifact-security",
        "multi-controller-routing",
        "local-vs-production-target",
    ),
    start=1,
)]
REQUIRED = [
    "README.md",
    "QUICKSTART.md",
    ".gitattributes",
    "api/openapi.yaml",
    "docker-compose.yml",
    "jenkins/casc/base.yaml",
    "templates/catalog.yaml",
    "backstage/netci-template.yaml",
    "backstage/app-config.example.yaml",
    "deploy/ansible/playbooks/deploy-kubernetes.yml",
    "deploy/ansible/playbooks/deploy-systemd.yml",
    "scripts/doctor.py",
    "scripts/validate_release.py",
    "release-checklist.yaml",
    *CORE_DOCS,
    *ADRS,
]
YAML_FILES = [
    "api/openapi.yaml",
    "docker-compose.yml",
    "templates/catalog.yaml",
    "backstage/netci-template.yaml",
    "backstage/app-config.example.yaml",
    "release-checklist.yaml",
]


def make_targets(text: str) -> set[str]:
    return set(re.findall(r"^([A-Za-z0-9_.-]+):(?:\s|$)", text, flags=re.MULTILINE))


errors: list[str] = []
for relative in REQUIRED:
    path = ROOT / relative
    if not path.is_file() and relative != ".git":
        errors.append(f"missing required file: {relative}")

for relative in YAML_FILES:
    try:
        yaml.safe_load((ROOT / relative).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        errors.append(f"invalid YAML {relative}: {exc}")

if not compileall.compile_dir(str(ROOT / "backend"), quiet=1):
    errors.append("backend compile failed")

package = json.loads((ROOT / "frontend/package.json").read_text(encoding="utf-8"))
lock = json.loads((ROOT / "frontend/package-lock.json").read_text(encoding="utf-8"))
for group in ("dependencies", "devDependencies"):
    for name, version in package.get(group, {}).items():
        if version in {"latest", "*"} or any(token in version for token in ("^", "~", ">", "<")):
            errors.append(f"frontend dependency is not exactly pinned: {name}={version}")
        if lock.get("packages", {}).get("", {}).get(group, {}).get(name) != version:
            errors.append(f"package-lock root does not match package.json: {name}")

client_text = (ROOT / "frontend/src/api/netciClient.ts").read_text(encoding="utf-8")
app_text = (ROOT / "frontend/src/App.tsx").read_text(encoding="utf-8")
vite_text = (ROOT / "frontend/vite.config.ts").read_text(encoding="utf-8")
if "configuredBaseUrl || '/api'" not in client_text:
    errors.append("Portal API client must default to the same-origin /api proxy")
for call in ("getStageCatalog()", "createModule(", "startModulePipeline("):
    if call not in client_text:
        errors.append(f"Portal is not wired to {call.rstrip('(')}")
if "rewrite:" not in vite_text or "replace(/^\\/api/" not in vite_text:
    errors.append("Vite /api proxy must strip the prefix before forwarding to FastAPI")

backstage = yaml.safe_load((ROOT / "backstage/netci-template.yaml").read_text(encoding="utf-8"))
parameter_groups = backstage.get("spec", {}).get("parameters", [])
required_parameters = {item for group in parameter_groups for item in group.get("required", [])}
if "runtime" not in required_parameters:
    errors.append("Backstage template must require runtime instead of hardcoding it")
backstage_text = (ROOT / "backstage/netci-template.yaml").read_text(encoding="utf-8")
if "runtime: docker" in backstage_text:
    errors.append("Backstage template hardcodes Docker runtime")
if "/api/proxy/netci/applications" not in backstage_text:
    errors.append("Backstage template does not use the documented netCI proxy")
if "output.body.url" in backstage_text:
    errors.append("Backstage output references URL not returned by netCI API")

makefile_text = (ROOT / "Makefile").read_text(encoding="utf-8")
targets = make_targets(makefile_text)
script_references = re.findall(r"(?:python3?|bash|\$\([A-Z_]+\))\s+([A-Za-z0-9_./-]+\.(?:py|sh))", makefile_text)
for relative in script_references:
    if not (ROOT / relative).is_file():
        errors.append(f"Makefile references missing script: {relative}")

for relative in ["README.md", "QUICKSTART.md", *CORE_DOCS]:
    text = (ROOT / relative).read_text(encoding="utf-8")
    for target in re.findall(r"\bmake\s+([A-Za-z0-9_.-]+)", text):
        if target not in targets:
            errors.append(f"{relative} references missing Makefile target: {target}")

try:
    errors.extend(validate_manifest(load_manifest()))
except (OSError, ValueError, yaml.YAMLError) as exc:
    errors.append(f"invalid release checklist: {exc}")

if errors:
    raise SystemExit("\n".join(dict.fromkeys(errors)))
print(f"validation passed: {len(REQUIRED)} required files, pinned frontend, wired clients and release references")
