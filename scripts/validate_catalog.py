from __future__ import annotations

import pathlib

import yaml


ROOT = pathlib.Path(__file__).resolve().parents[1]
CATALOG = ROOT / "templates" / "catalog.yaml"
EXPECTED_TEMPLATES = {
    "container-ci-cd-v1": "docker",
    "kubernetes-ci-cd-v1": "kubernetes",
    "systemd-ansible-ci-cd-v1": "systemd",
}
KNOWN_STAGES = {
    "checkout",
    "unit-test",
    "build",
    "sbom",
    "vulnerability-scan",
    "sign",
    "publish",
    "deploy",
    "health-check",
}
REQUIRED_STAGES = {"checkout", "unit-test", "build", "publish", "deploy", "health-check"}
CONTAINER_SECURITY_STAGES = {"sbom", "vulnerability-scan", "sign"}


catalog = yaml.safe_load(CATALOG.read_text(encoding="utf-8"))
items = catalog.get("templates", []) if isinstance(catalog, dict) else []
errors: list[str] = []
ids: list[str] = []

if not items:
    errors.append("catalog must contain templates")
for item in items:
    template_id = item.get("id", "<unknown>")
    ids.append(template_id)
    expected_runtime = EXPECTED_TEMPLATES.get(template_id)
    if expected_runtime is None:
        errors.append(f"{template_id}: unexpected template id")
    elif item.get("runtime") != expected_runtime:
        errors.append(f"{template_id}: expected runtime {expected_runtime}")
    if item.get("deployAdapter") != item.get("runtime"):
        errors.append(f"{template_id}: deployAdapter must match runtime")

    stages = item.get("stages", [])
    if not isinstance(stages, list) or len(stages) != len(set(stages)):
        errors.append(f"{template_id}: stages must be a unique list")
        continue
    unknown = set(stages) - KNOWN_STAGES
    missing = REQUIRED_STAGES - set(stages)
    if unknown:
        errors.append(f"{template_id}: unknown stages {sorted(unknown)}")
    if missing:
        errors.append(f"{template_id}: missing stages {sorted(missing)}")
    if item.get("artifactKind") == "container-image":
        security_missing = CONTAINER_SECURITY_STAGES - set(stages)
        if security_missing:
            errors.append(f"{template_id}: missing container security stages {sorted(security_missing)}")
    if "publish" in stages and "deploy" in stages and stages.index("publish") > stages.index("deploy"):
        errors.append(f"{template_id}: deploy must occur after publish")
    if "deploy" in stages and "health-check" in stages and stages.index("deploy") > stages.index("health-check"):
        errors.append(f"{template_id}: health-check must occur after deploy")
    if set(item.get("environments", [])) != {"dev", "staging", "prod"}:
        errors.append(f"{template_id}: environments must be dev/staging/prod")

if len(ids) != len(set(ids)):
    errors.append("template ids must be unique")
if set(ids) != set(EXPECTED_TEMPLATES):
    errors.append(f"catalog template set must be {sorted(EXPECTED_TEMPLATES)}")

if errors:
    raise SystemExit("\n".join(errors))
print(f"catalog valid: {len(items)} templates, unique stages and runtime/adapter invariants")
