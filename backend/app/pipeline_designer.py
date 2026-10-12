"""Pipeline designer domain logic (ADR-057).

Pure domain functions for validating proposals, rendering and parsing `.netci/pipeline.yaml`,
and resolving stage code.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from .domain.models import StageDefinition

# Custom stage ids: lowercase letters, digits, dashes (2 to 41 chars)
CUSTOM_STAGE_ID_RE = re.compile(r"^[a-z][a-z0-9-]{1,40}$")

STAGE_FILE_MAP: dict[str, str] = {
    "unit-test": "test.sh",
    "build": "build.sh",
    "sbom": "sbom.sh",
    "vulnerability-scan": "scan.sh",
    "sign": "sign.sh",
    "publish": "publish.sh",
}

MAX_STAGE_CODE_BYTES = 65536


def stage_script_path(stage_id: str) -> str:
    """Where a module's custom stage script lives in its repository -- always here.

    The path is derived, never stored from input: a `script` of `../../ci/deploy.sh` or
    an absolute path would make the build run a file nobody reviewed as a stage.
    """
    return f".netci/stages/{stage_id}.sh"


class PipelineProposalError(ValueError):
    """Refusal of a pipeline proposal according to ADR-057 contract."""

    def __init__(self, code: str, message: str, status_code: int = 422) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def current_pipeline(
    application_stages: tuple[str, ...] | list[str],
    catalog: dict[str, StageDefinition],
    module_custom_stages: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Return the module's current pipeline in run order.

    Built-ins from the application's stages in template order, custom ones from
    pipelineConfig.customStages placed after their anchor.
    """
    custom_by_anchor: dict[str, list[dict[str, Any]]] = {}
    for c in module_custom_stages or []:
        anchor = c.get("after")
        if anchor:
            custom_by_anchor.setdefault(anchor, []).append(c)

    ordered: list[dict[str, Any]] = []
    for stage_id in application_stages:
        stage_def = catalog.get(stage_id)
        if stage_def is not None and stage_def.kind == "builtin":
            ordered.append({
                "id": stage_def.id,
                "name": stage_def.name,
                "category": stage_def.category,
                "kind": "builtin",
                "required": stage_def.required,
                "after": None,
                "script": None,
            })
        elif stage_def is not None:
            # Custom stage already in catalog
            ordered.append({
                "id": stage_def.id,
                "name": stage_def.name,
                "category": stage_def.category,
                "kind": "custom",
                "required": stage_def.required,
                "after": stage_def.after_stage,
                "script": stage_def.script or f".netci/stages/{stage_def.id}.sh",
            })
        else:
            # Stage ID not in catalog, treat as builtin placeholder
            ordered.append({
                "id": stage_id,
                "name": stage_id,
                "category": "build",
                "kind": "builtin",
                "required": False,
                "after": None,
                "script": None,
            })

        for custom in custom_by_anchor.get(stage_id, []):
            ordered.append({
                "id": custom["id"],
                "name": custom.get("name") or custom["id"],
                "category": "custom",
                "kind": "custom",
                "required": False,
                "after": custom.get("after"),
                "script": stage_script_path(custom["id"]),
            })

    return ordered


def validate_proposal(
    catalog: dict[str, StageDefinition],
    template_order: tuple[str, ...] | list[str],
    submitted_stages: list[dict[str, Any]],
    *,
    require_code: bool = True,
) -> list[dict[str, Any]]:
    """Validate submitted proposal stages and return them in validated run order.

    Rules:
    - built-in ids must be catalog built-ins;
    - every required built-in must be present;
    - built-ins keep template order (reordering them is 422 PIPELINE_ORDER_INVALID);
    - custom ids match ^[a-z][a-z0-9-]{1,40}$ and are not catalog ids (PIPELINE_STAGE_ID_TAKEN);
    - a custom stage needs "after" naming a built-in present in the list and "code"
      (<= 65536 bytes, no NUL, UTF-8; PIPELINE_STAGE_CODE_INVALID);
    - a built-in carrying "after" or "code" is refused, not stripped: dropping them would
      answer 201 for code that is never written anywhere.

    `require_code=False` is for a merged `.netci/pipeline.yaml`, which names stages but
    carries no code (the scripts are files beside it).
    """
    if not submitted_stages:
        raise PipelineProposalError("PIPELINE_ORDER_INVALID", "Proposal stages list cannot be empty")

    seen_ids: set[str] = set()
    for s in submitted_stages:
        if not isinstance(s, dict):
            raise PipelineProposalError("PIPELINE_STAGE_CODE_INVALID", "Stage entry must be an object")
        sid = s.get("id")
        if not isinstance(sid, str) or not sid:
            raise PipelineProposalError("PIPELINE_STAGE_CODE_INVALID", "Stage entry must have an 'id'")
        if sid in seen_ids:
            raise PipelineProposalError("PIPELINE_STAGE_ID_TAKEN", f"Duplicate stage id: {sid}")
        seen_ids.add(sid)

    builtins_submitted: list[str] = []
    customs_submitted: list[dict[str, Any]] = []

    for s in submitted_stages:
        sid = s["id"]
        cat_stage = catalog.get(sid)
        if cat_stage is not None and cat_stage.kind == "builtin":
            if s.get("after") is not None or s.get("code") is not None:
                raise PipelineProposalError(
                    "PIPELINE_STAGE_ID_TAKEN",
                    f"'{sid}' is a built-in stage: it takes no 'after' or 'code'; "
                    "choose another id for a custom stage",
                )
            builtins_submitted.append(sid)
        else:
            # Custom stage
            if sid in catalog:
                raise PipelineProposalError(
                    "PIPELINE_STAGE_ID_TAKEN",
                    f"Stage id '{sid}' is already taken in the stage catalog",
                )
            if not CUSTOM_STAGE_ID_RE.fullmatch(sid):
                raise PipelineProposalError(
                    "PIPELINE_STAGE_ID_TAKEN",
                    f"Custom stage id '{sid}' must match ^[a-z][a-z0-9-]{{1,40}}$",
                )
            customs_submitted.append(s)

    # Validate template order of built-ins
    positions = {sid: idx for idx, sid in enumerate(template_order)}
    for b_id in builtins_submitted:
        if b_id not in positions:
            raise PipelineProposalError(
                "PIPELINE_ORDER_INVALID",
                f"Built-in stage '{b_id}' is not part of this template",
            )

    builtins_indices = [positions[b_id] for b_id in builtins_submitted]
    if builtins_indices != sorted(builtins_indices):
        raise PipelineProposalError(
            "PIPELINE_ORDER_INVALID",
            "Built-in stages must keep the template's order",
        )

    # Validate all required built-ins are present
    missing_required = [
        sid for sid in template_order
        if catalog.get(sid) is not None and catalog[sid].required and sid not in builtins_submitted
    ]
    if missing_required:
        raise PipelineProposalError(
            "PIPELINE_ORDER_INVALID",
            f"Required stages cannot be removed: {', '.join(missing_required)}",
        )

    # Validate custom stages
    builtins_set = set(builtins_submitted)
    custom_by_anchor: dict[str, list[dict[str, Any]]] = {}

    for c in customs_submitted:
        cid = c["id"]
        after = c.get("after")
        if not after or after not in builtins_set:
            raise PipelineProposalError(
                "PIPELINE_STAGE_CODE_INVALID",
                f"Custom stage '{cid}' must specify an 'after' anchor naming an active built-in stage in the pipeline",
            )

        code = c.get("code")
        if code is None and not require_code:
            custom_by_anchor.setdefault(after, []).append(c)
            continue
        if code is None or not isinstance(code, str):
            raise PipelineProposalError(
                "PIPELINE_STAGE_CODE_INVALID",
                f"Custom stage '{cid}' must provide executable bash script code",
            )
        code_bytes = code.encode("utf-8")
        if len(code_bytes) > MAX_STAGE_CODE_BYTES:
            raise PipelineProposalError(
                "PIPELINE_STAGE_CODE_INVALID",
                f"Custom stage '{cid}' code exceeds maximum size of {MAX_STAGE_CODE_BYTES} bytes",
            )
        if "\x00" in code:
            raise PipelineProposalError(
                "PIPELINE_STAGE_CODE_INVALID",
                f"Custom stage '{cid}' code contains NUL bytes",
            )

        custom_by_anchor.setdefault(after, []).append(c)

    # Construct the validated order: builtins in template order, custom stages after their anchor
    validated_order: list[dict[str, Any]] = []
    for b_id in builtins_submitted:
        b_stage = catalog.get(b_id)
        name = b_stage.name if b_stage else b_id
        validated_order.append({
            "id": b_id,
            "name": name,
            "after": None,
            "code": None,
        })
        for c in custom_by_anchor.get(b_id, []):
            validated_order.append({
                "id": c["id"],
                "name": c.get("name") or c["id"],
                "after": c["after"],
                "code": c.get("code"),
            })

    return validated_order


def module_custom_stages(
    pipeline_config: dict[str, Any] | None,
    template_order: tuple[str, ...] | list[str] | None = None,
) -> list[dict[str, Any]]:
    """`pipelineConfig.customStages`, checked: what a module adds beyond the catalog.

    Every writer of a config revision passes through here -- the merge webhook and a
    hand-edited revision alike -- so a revision cannot carry a stage the designer would
    have refused. Raises ValueError with the reason.
    """
    raw = (pipeline_config or {}).get("customStages")
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError("customStages must be a list")
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("each customStages entry must be an object")
        unknown = set(item) - {"id", "name", "after", "script"}
        if unknown:
            raise ValueError(f"customStages: unknown field(s) {sorted(unknown)}")
        sid = item.get("id")
        if not isinstance(sid, str) or not CUSTOM_STAGE_ID_RE.fullmatch(sid):
            raise ValueError(f"customStages: id {sid!r} must match ^[a-z][a-z0-9-]{{1,40}}$")
        if sid in seen:
            raise ValueError(f"customStages: {sid} is listed twice")
        seen.add(sid)
        after = item.get("after")
        if not isinstance(after, str) or not after:
            raise ValueError(f"customStages: {sid} needs 'after', the built-in stage it runs after")
        if template_order is not None and after not in template_order:
            raise ValueError(f"customStages: {sid} runs after {after!r}, which is not a stage of this template")
        # Refused rather than overridden: a silent rewrite is indistinguishable from the
        # submitted path having been accepted.
        script = item.get("script")
        if script is not None and script != stage_script_path(sid):
            raise ValueError(f"customStages: {sid} script must be {stage_script_path(sid)}")
        name = item.get("name")
        if name is not None and (not isinstance(name, str) or len(name) > 120):
            raise ValueError(f"customStages: {sid} name must be a string of at most 120 characters")
        out.append({"id": sid, "name": name or sid, "after": after, "script": stage_script_path(sid)})
    return out


def custom_stage_definitions(custom_stages: list[dict[str, Any]]) -> list[StageDefinition]:
    """A module's custom stages as catalog entries, so stage resolution and the CI launch
    treat them like any other custom stage. They are active: the merge that added them
    was their review (ADR-057)."""
    return [
        StageDefinition(
            id=c["id"],
            name=c.get("name") or c["id"],
            category="custom",
            kind="custom",
            position=100,
            script=stage_script_path(c["id"]),
            after_stage=c["after"],
            status="active",
            parameters=(),
        )
        for c in custom_stages
    ]


def render_pipeline_yaml(stages: list[dict[str, Any]]) -> str:
    """Render the stages list into `.netci/pipeline.yaml` format."""
    out_stages: list[dict[str, Any]] = []
    for s in stages:
        item: dict[str, Any] = {"id": s["id"]}
        if s.get("after"):
            item["after"] = s["after"]
        if s.get("name"):
            item["name"] = s["name"]
        out_stages.append(item)

    data = {
        "version": 1,
        "stages": out_stages,
    }
    return yaml.safe_dump(data, sort_keys=False)


def parse_pipeline_yaml(text: str) -> dict[str, Any]:
    """Parse `.netci/pipeline.yaml` strictly.

    Rejects unknown keys, bad versions, or malformed stage declarations.
    """
    try:
        data = yaml.safe_load(text)
    except Exception as exc:
        raise ValueError(f"Invalid YAML syntax: {exc}") from exc

    if not isinstance(data, dict):
        raise ValueError("Root of pipeline.yaml must be a mapping")

    allowed_root_keys = {"version", "stages"}
    unknown_root_keys = set(data.keys()) - allowed_root_keys
    if unknown_root_keys:
        raise ValueError(f"Unknown root key(s) in pipeline.yaml: {sorted(unknown_root_keys)}")

    if data.get("version") != 1:
        raise ValueError(f"Unsupported pipeline.yaml version: {data.get('version')}")

    stages = data.get("stages")
    if not isinstance(stages, list):
        raise ValueError("'stages' must be a list")

    parsed_stages: list[dict[str, Any]] = []
    allowed_stage_keys = {"id", "after", "name"}

    for s in stages:
        if not isinstance(s, dict):
            raise ValueError("Each stage in 'stages' must be a mapping")
        unknown_stage_keys = set(s.keys()) - allowed_stage_keys
        if unknown_stage_keys:
            raise ValueError(f"Unknown key(s) in stage definition: {sorted(unknown_stage_keys)}")

        sid = s.get("id")
        if not isinstance(sid, str) or not sid:
            raise ValueError("Stage must have a non-empty string 'id'")
        if not re.match(r"^[a-z][a-z0-9-]{1,62}$", sid):
            raise ValueError(f"Invalid stage id '{sid}'")

        after = s.get("after")
        if after is not None:
            if not isinstance(after, str) or not re.match(r"^[a-z][a-z0-9-]{1,62}$", after):
                raise ValueError(f"Invalid 'after' stage anchor '{after}'")

        name = s.get("name")
        if name is not None and not isinstance(name, str):
            raise ValueError("Stage 'name' must be a string")

        parsed_stage: dict[str, Any] = {"id": sid}
        if after is not None:
            parsed_stage["after"] = after
        if name is not None:
            parsed_stage["name"] = name
        parsed_stages.append(parsed_stage)

    return {"version": 1, "stages": parsed_stages}


_TEMPLATES_RELATIVE = Path("jenkins") / "shared-library" / "resources" / "netci" / "tooling" / "templates"


def _templates_dir() -> Path | None:
    """The shared library's templates: <repo>/jenkins/... in a checkout, /app/jenkins/...
    in the image (backend/Dockerfile copies them), the same two layouts toolchain.py
    handles."""
    here = Path(__file__).resolve()
    for root in (here.parents[2], here.parents[1]):
        candidate = root / _TEMPLATES_RELATIVE
        if candidate.is_dir():
            return candidate
    return None


def builtin_stage_code(template: str, stage_id: str) -> str:
    """Read the template's bash script for a built-in stage, preventing path traversal."""
    if stage_id == "checkout":
        return "# Checkout is handled directly by the pipeline itself."
    if stage_id in ("deploy", "health-check"):
        return f"# Stage '{stage_id}' is executed by the netCI runtime worker/orchestrator."

    file_name = STAGE_FILE_MAP.get(stage_id)
    if not file_name:
        return ""

    # Sanitize template name: only lowercase letters, digits, and dashes allowed
    if not re.match(r"^[a-z0-9-]+$", template):
        return ""

    base_dir = _templates_dir()
    if base_dir is None:
        return ""
    target_file = (base_dir / template / "scripts" / "ci" / file_name).resolve()

    # Path traversal check
    if not str(target_file).startswith(str(base_dir.resolve())):
        return ""

    if not target_file.is_file():
        return ""

    return target_file.read_text(encoding="utf-8")


def builtin_stage_path(template: str, stage_id: str) -> str:
    """Return the repository-relative path for a built-in stage."""
    file_name = STAGE_FILE_MAP.get(stage_id)
    if file_name:
        return f"jenkins/shared-library/resources/netci/tooling/templates/{template}/scripts/ci/{file_name}"
    if stage_id == "checkout":
        return "jenkins/shared-library/vars/netciPipeline.groovy"
    return f"jenkins/shared-library/resources/netci/tooling/templates/{template}/scripts/{stage_id}.sh"
