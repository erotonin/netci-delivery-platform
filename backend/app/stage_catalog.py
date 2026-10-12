"""The stage catalog: what a module's pipeline may consist of, and in what order.

Developers configure a pipeline by choosing from this catalog in the portal; the
shared Jenkins pipeline reads the choice from the build parameters. Nobody edits a
Jenkinsfile. Two kinds of entry:

- **built-in** stages are the ones `jenkins/shared-library/vars/netciPipeline.groovy`
  implements; their order is the template's order and some of them are *required*,
  because an artifact without checkout, build, SBOM, scan, signature and publish is not
  something netCI will deploy;
- **custom** stages are registered by a platform administrator. A custom stage runs
  one script that lives in the application's repository (so it is reviewed in git like
  any other code) in the builder container, right after the built-in stage it is
  anchored to. The portal never accepts a command: only a repository-relative path.

The catalog lives in the database (migration 0021); the tuple below is the built-in
set the in-memory store starts from and the source the migration was written from.
"""

from __future__ import annotations

import re
from dataclasses import replace

from .domain.models import StageDefinition, utc_now

STAGE_ID = re.compile(r"^[a-z][a-z0-9-]{1,62}$")
# A path inside the repository: no leading slash, no `..`, ends in .sh. The pipeline
# runs it with bash from the workspace root, in the builder container, with the same
# environment the built-in scripts see.
SCRIPT_PATH = re.compile(r"^(?!.*(?:^|/)\.\.(?:/|$))[A-Za-z0-9._][A-Za-z0-9._/-]{0,253}\.sh$")
CATEGORIES = ("source", "test", "build", "security", "publish", "deploy", "verify", "custom")

BUILTIN_STAGES: tuple[StageDefinition, ...] = (
    StageDefinition("checkout", "Checkout source", "source", "builtin", 10, "Clone the commit netCI named; nothing else is built.", required=True),
    StageDefinition("unit-test", "Unit tests", "test", "builtin", 20, "Run the template's test script in the builder."),
    StageDefinition("build", "Build artifact/image", "build", "builtin", 30, "Build the image or binary from the checked-out source.", required=True),
    StageDefinition("sbom", "Generate SBOM", "security", "builtin", 40, "Syft SBOM of the artifact; netCI's policy requires it.", required=True),
    StageDefinition("vulnerability-scan", "Vulnerability scan", "security", "builtin", 50, "Trivy scan; critical and high findings deny the artifact.", required=True),
    StageDefinition("sign", "Sign artifact", "publish", "builtin", 60, "Push to the registry and sign the digest with cosign.", required=True),
    StageDefinition("publish", "Publish artifact", "publish", "builtin", 70, "Check the evidence agrees with the artifact and submit it to netCI.", required=True),
    StageDefinition("deploy", "Deploy through netCI", "deploy", "builtin", 80, "netCI starts the deployment workflow once the artifact is admitted."),
    StageDefinition("health-check", "Health check", "verify", "builtin", 90, "The runtime playbook's health gate; a release that fails it is rolled back."),
)

#: Built-in stages custom stages may be anchored after. `publish` is the last stage that
#: runs on the build agent; deploy and health-check happen in netCI's worker.
ANCHORS = ("checkout", "unit-test", "build", "sbom", "vulnerability-scan", "sign", "publish")


class StageCatalogError(ValueError):
    def __init__(self, code: str, message: str, status_code: int = 422) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


PARAMETER_NAME = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
# A value reaches the stage script as an environment variable. Shell metacharacters are
# refused not because the pipeline would evaluate them (it does not: `withEnv`), but so
# a value can never become a command if a script author interpolates it carelessly.
PARAMETER_VALUE = re.compile(r"^[A-Za-z0-9._:/@=,+ -]{0,256}$")
MAX_PARAMETERS = 16


def validate_parameter_declarations(declared: list[dict] | tuple[dict, ...] | None) -> tuple[dict[str, str], ...]:
    """The parameters a custom stage declares: name, default, description."""

    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in declared or ():
        if not isinstance(item, dict):
            raise StageCatalogError("INVALID_STAGE_PARAMETER", "a parameter is an object with name, default, description")
        name = str(item.get("name") or "")
        if not PARAMETER_NAME.fullmatch(name):
            raise StageCatalogError("INVALID_STAGE_PARAMETER", f"parameter name {name!r} must be UPPER_SNAKE (max 64 chars)")
        if name in seen:
            raise StageCatalogError("INVALID_STAGE_PARAMETER", f"parameter {name} declared twice")
        seen.add(name)
        default = str(item.get("default") or "")
        if not PARAMETER_VALUE.fullmatch(default):
            raise StageCatalogError("INVALID_STAGE_PARAMETER", f"default for {name} contains characters a value may not carry")
        out.append({"name": name, "default": default, "description": str(item.get("description") or "")[:500]})
        if len(out) > MAX_PARAMETERS:
            raise StageCatalogError("INVALID_STAGE_PARAMETER", f"at most {MAX_PARAMETERS} parameters per stage")
    return tuple(out)


def validate_stage_parameters(
    stage_ids: tuple[str, ...], values: dict[str, dict[str, str]] | None, catalog: dict[str, StageDefinition]
) -> dict[str, dict[str, str]]:
    """A module's values for its custom stages: only declared names, only safe values."""

    out: dict[str, dict[str, str]] = {}
    for stage_id, given in (values or {}).items():
        if stage_id not in stage_ids:
            raise StageCatalogError("INVALID_STAGE_PARAMETER", f"parameters given for {stage_id}, which is not in the pipeline")
        stage = catalog.get(stage_id)
        if stage is None or stage.kind != "custom":
            raise StageCatalogError("INVALID_STAGE_PARAMETER", f"{stage_id} takes no parameters")
        declared = {p["name"] for p in stage.parameters}
        if not isinstance(given, dict):
            raise StageCatalogError("INVALID_STAGE_PARAMETER", f"parameters for {stage_id} must be an object")
        clean: dict[str, str] = {}
        for name, value in given.items():
            if name not in declared:
                raise StageCatalogError("INVALID_STAGE_PARAMETER", f"{stage_id} declares no parameter {name}")
            if not isinstance(value, str) or not PARAMETER_VALUE.fullmatch(value):
                raise StageCatalogError("INVALID_STAGE_PARAMETER", f"value for {stage_id}.{name} contains characters a value may not carry")
            clean[name] = value
        if clean:
            out[stage_id] = clean
    return out


def custom_stage(
    *, stage_id: str, name: str, script: str, after_stage: str, description: str = "",
    category: str = "custom", created_by: str, parameters: list[dict] | None = None,
    status: str = "proposed",
) -> StageDefinition:
    """Validate an administrator's registration; the result is what the store keeps."""

    if not STAGE_ID.fullmatch(stage_id):
        raise StageCatalogError("INVALID_STAGE_ID", "stage id must be lowercase letters, digits and dashes (2-63 chars)")
    if any(stage_id == builtin.id for builtin in BUILTIN_STAGES):
        raise StageCatalogError("STAGE_ID_RESERVED", f"{stage_id!r} is a built-in stage", 409)
    if not SCRIPT_PATH.fullmatch(script):
        raise StageCatalogError(
            "INVALID_STAGE_SCRIPT",
            "script must be a repository-relative path ending in .sh, without '..' or a leading slash",
        )
    if after_stage not in ANCHORS:
        raise StageCatalogError("INVALID_STAGE_ANCHOR", f"a custom stage runs after one of: {', '.join(ANCHORS)}")
    if category not in CATEGORIES:
        raise StageCatalogError("INVALID_STAGE_CATEGORY", f"category must be one of: {', '.join(CATEGORIES)}")
    if not name.strip() or len(name) > 120:
        raise StageCatalogError("INVALID_STAGE_NAME", "name is required (at most 120 characters)")
    anchor = next(b for b in BUILTIN_STAGES if b.id == after_stage)
    now = utc_now()
    return StageDefinition(
        id=stage_id, name=name.strip(), category=category, kind="custom",
        # Between the anchor and the next built-in, so a listing in position order
        # already shows where it runs.
        position=anchor.position + 5, description=description.strip(), script=script,
        after_stage=after_stage, required=False, enabled_by_default=False,
        created_by=created_by, created_at=now, updated_at=now,
        status=status, parameters=validate_parameter_declarations(parameters),
    )


def resolve_pipeline_stages(
    template_stages: tuple[str, ...], chosen: list[str], catalog: dict[str, StageDefinition]
) -> tuple[str, ...]:
    """The stage list a run will carry, from what a module chose.

    Built-ins keep the template's order and the required ones cannot be left out;
    custom stages must be in the catalog and land right after their anchor, in the
    order the module listed them. The result is what `NETCI_STAGES` names.
    """

    if not chosen:
        return template_stages
    if len(chosen) != len(set(chosen)):
        raise StageCatalogError("INVALID_STAGES", "a stage may be listed once")
    unknown = [stage_id for stage_id in chosen if stage_id not in catalog]
    if unknown:
        raise StageCatalogError("UNKNOWN_STAGE", f"not in the stage catalog: {', '.join(unknown)}")
    builtins_chosen = [stage_id for stage_id in chosen if catalog[stage_id].kind == "builtin"]
    not_in_template = [stage_id for stage_id in builtins_chosen if stage_id not in template_stages]
    if not_in_template:
        raise StageCatalogError("INVALID_STAGES", f"not part of this template: {', '.join(not_in_template)}")
    positions = {stage_id: index for index, stage_id in enumerate(template_stages)}
    if [positions[s] for s in builtins_chosen] != sorted(positions[s] for s in builtins_chosen):
        raise StageCatalogError("INVALID_STAGES", "built-in stages must keep the template's order")
    missing_required = [
        stage_id for stage_id in template_stages
        if catalog.get(stage_id) is not None and catalog[stage_id].required and stage_id not in builtins_chosen
    ]
    if missing_required:
        raise StageCatalogError(
            "REQUIRED_STAGE_REMOVED",
            f"these stages cannot be removed, they are what makes an artifact deployable: {', '.join(missing_required)}",
        )
    customs = [catalog[stage_id] for stage_id in chosen if catalog[stage_id].kind == "custom"]
    # A stage another administrator has not yet approved is not part of any pipeline.
    inactive = [c.id for c in customs if c.status != "active"]
    if inactive:
        raise StageCatalogError("STAGE_NOT_ACTIVE", f"not approved for use: {', '.join(inactive)}")
    orphaned = [c.id for c in customs if c.after_stage not in builtins_chosen]
    if orphaned:
        raise StageCatalogError("STAGE_ANCHOR_DISABLED", f"anchored after a stage that is not enabled: {', '.join(orphaned)}")
    resolved: list[str] = []
    for stage_id in template_stages:
        if stage_id not in builtins_chosen:
            continue
        resolved.append(stage_id)
        resolved.extend(c.id for c in customs if c.after_stage == stage_id)
    return tuple(resolved)


def custom_stage_parameters(
    stage_ids: tuple[str, ...], catalog: dict[str, StageDefinition],
    values: dict[str, dict[str, str]] | None = None,
) -> list[dict[str, object]]:
    """What the pipeline needs to run the custom stages in a run's list.

    `env` is the stage's declared parameters with the module's values over the defaults,
    handed to the script as environment variables (`withEnv`), never interpolated.
    """

    out: list[dict[str, object]] = []
    for stage_id in stage_ids:
        s = catalog.get(stage_id)
        if s is None or s.kind != "custom":
            continue
        env = {p["name"]: p.get("default", "") for p in s.parameters}
        env.update((values or {}).get(stage_id, {}))
        out.append({"id": s.id, "name": s.name, "script": s.script or "", "after": s.after_stage or "", "env": env})
    return out


def touch(stage: StageDefinition) -> StageDefinition:
    return replace(stage, updated_at=utc_now())


def stage_json(stage: StageDefinition) -> dict[str, object]:
    return {
        "id": stage.id,
        "name": stage.name,
        "category": stage.category,
        "kind": stage.kind,
        "description": stage.description,
        "script": stage.script,
        "afterStage": stage.after_stage,
        "required": stage.required,
        "enabledByDefault": stage.enabled_by_default,
        "position": stage.position,
        "createdBy": stage.created_by,
        "createdAt": stage.created_at.isoformat(),
        "updatedAt": stage.updated_at.isoformat(),
        "status": stage.status,
        "approvedBy": stage.approved_by,
        "parameters": [dict(p) for p in stage.parameters],
    }
