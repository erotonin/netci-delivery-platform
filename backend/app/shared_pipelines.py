"""Shared pipelines (ADR-058): one script, cut into stages by marker lines.

    # @stage unit-test "Unit Tests" builtin
    netci-builtin unit-test
    # @stage lint "Lint Dockerfile"
    hadolint Dockerfile

A `builtin` block names a stage netCI implements and runs with only the credentials that stage
needs; its body must be exactly `netci-builtin <id>`. Any other block is the author's bash and
runs in the builder with no credentials. Nothing here does I/O.
"""

from __future__ import annotations

import base64
import hashlib
import re
from dataclasses import dataclass

#: Built-in CI stages a pipeline may name, in the only order they run.
CI_BUILTINS: tuple[str, ...] = ("unit-test", "build", "sbom", "vulnerability-scan", "sign", "publish")
#: The ones that make an artifact deployable (the evidence gates read their output).
REQUIRED_BUILTINS: tuple[str, ...] = ("build", "sbom", "vulnerability-scan", "sign", "publish")
#: Stages that are not CI: checkout always runs first, deploy and health-check are CD.
NOT_IN_A_PIPELINE: dict[str, str] = {
    "checkout": "checkout always runs first; it is not part of the script",
    "deploy": "deploy is CD, done by netCI's worker for each module and runtime",
    "health-check": "health-check is CD, done by netCI's worker after a deployment",
}
BUILTIN_NAMES: dict[str, str] = {
    "unit-test": "Unit Tests",
    "build": "Build",
    "sbom": "Generate SBOM",
    "vulnerability-scan": "Vulnerability Scan",
    "sign": "Sign Artifact",
    "publish": "Publish Artifact",
}

PIPELINE_NAME = re.compile(r"^[a-z][a-z0-9-]{1,62}$")
STAGE_ID = re.compile(r"^[a-z][a-z0-9-]{1,40}$")
MARKER = re.compile(r'^# @stage (?P<id>\S+) "(?P<name>[^"\n]{1,80})"(?P<builtin> builtin)?[ \t]*$')
MAX_SCRIPT_BYTES = 65536


class PipelineScriptError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class Block:
    id: str
    name: str
    builtin: bool
    body: str


def script_sha256(script: str) -> str:
    return hashlib.sha256(script.encode("utf-8")).hexdigest()


def builtin_block(stage_id: str) -> str:
    """The text a builtin stage contributes when clicked in the designer."""
    return f'# @stage {stage_id} "{BUILTIN_NAMES[stage_id]}" builtin\nnetci-builtin {stage_id}\n'


def parse(script: str) -> list[Block]:
    """Blocks of a pipeline script, validated; raises PipelineScriptError with the reason."""
    if not isinstance(script, str) or not script.strip():
        raise PipelineScriptError("PIPELINE_SCRIPT_INVALID", "the pipeline script is empty")
    if len(script.encode("utf-8")) > MAX_SCRIPT_BYTES:
        raise PipelineScriptError("PIPELINE_SCRIPT_INVALID", f"the pipeline script is over {MAX_SCRIPT_BYTES} bytes")
    if "\x00" in script:
        raise PipelineScriptError("PIPELINE_SCRIPT_INVALID", "the pipeline script contains a NUL byte")

    blocks: list[tuple[str, str, bool, list[str]]] = []
    for number, line in enumerate(script.splitlines(), start=1):
        if line.startswith("# @stage"):
            match = MARKER.match(line)
            if match is None:
                raise PipelineScriptError(
                    "PIPELINE_SCRIPT_INVALID",
                    f'line {number}: a stage marker is `# @stage <id> "<Name>"` or `... builtin`',
                )
            blocks.append((match["id"], match["name"], bool(match["builtin"]), []))
        elif not blocks:
            # Code before the first stage would run nowhere -- or everywhere; refuse it.
            if line.strip() and not line.lstrip().startswith("#"):
                raise PipelineScriptError(
                    "PIPELINE_SCRIPT_INVALID", f"line {number}: code before the first `# @stage` belongs to no stage"
                )
        else:
            blocks[-1][3].append(line)
    if not blocks:
        raise PipelineScriptError("PIPELINE_SCRIPT_INVALID", "the script has no `# @stage` blocks")

    out = [Block(i, n, b, "\n".join(body).strip("\n")) for i, n, b, body in blocks]
    _validate(out)
    return out


def _validate(blocks: list[Block]) -> None:
    seen: set[str] = set()
    builtins: list[str] = []
    for block in blocks:
        if block.id in seen:
            raise PipelineScriptError("PIPELINE_STAGE_DUPLICATE", f"stage {block.id!r} appears twice")
        seen.add(block.id)
        if block.id in NOT_IN_A_PIPELINE:
            raise PipelineScriptError("PIPELINE_STAGE_NOT_CI", NOT_IN_A_PIPELINE[block.id])
        if block.builtin:
            if block.id not in CI_BUILTINS:
                raise PipelineScriptError(
                    "PIPELINE_STAGE_UNKNOWN", f"{block.id!r} is not a built-in stage ({', '.join(CI_BUILTINS)})"
                )
            # Refused, not overwritten: a builtin's body is netCI's code, and an edit that
            # was silently ignored would look as if it had run.
            meaningful = [ln.strip() for ln in block.body.splitlines() if ln.strip() and not ln.strip().startswith("#")]
            if meaningful != [f"netci-builtin {block.id}"]:
                raise PipelineScriptError(
                    "PIPELINE_BUILTIN_EDITED",
                    f"built-in stage {block.id!r} must contain only `netci-builtin {block.id}`; "
                    "put your own commands in a separate stage",
                )
            builtins.append(block.id)
        else:
            if block.id in CI_BUILTINS:
                raise PipelineScriptError(
                    "PIPELINE_STAGE_RESERVED", f"{block.id!r} is a built-in stage: mark it `builtin` or choose another id"
                )
            if not STAGE_ID.match(block.id):
                raise PipelineScriptError("PIPELINE_STAGE_INVALID", f"stage id {block.id!r} must match {STAGE_ID.pattern}")
            if not block.body.strip():
                raise PipelineScriptError("PIPELINE_STAGE_EMPTY", f"stage {block.id!r} has no commands")
    order = [CI_BUILTINS.index(b) for b in builtins]
    if order != sorted(order):
        raise PipelineScriptError(
            "PIPELINE_ORDER_INVALID", f"built-in stages must keep the order {' -> '.join(CI_BUILTINS)}"
        )
    missing = [r for r in REQUIRED_BUILTINS if r not in builtins]
    if missing:
        raise PipelineScriptError(
            "PIPELINE_REQUIRED_MISSING",
            f"these stages make an artifact deployable and cannot be left out: {', '.join(missing)}",
        )


def run_parameters(blocks: list[Block]) -> tuple[tuple[str, ...], list[dict[str, object]]]:
    """(stage ids for NETCI_STAGES, author blocks for NETCI_CUSTOM_STAGES).

    Each author block runs after the built-in before it (checkout when there is none), which
    is where the library's custom-stage slots are.
    """
    stages: list[str] = ["checkout"]
    custom: list[dict[str, object]] = []
    anchor = "checkout"
    for block in blocks:
        stages.append(block.id)
        if block.builtin:
            anchor = block.id
            continue
        code = "#!/usr/bin/env bash\nset -euo pipefail\n" + block.body + "\n"
        custom.append({
            "id": block.id, "name": block.name, "after": anchor, "env": {},
            "code": base64.b64encode(code.encode("utf-8")).decode("ascii"),
        })
    return tuple(stages), custom


def stages_summary(blocks: list[Block]) -> list[dict[str, object]]:
    return [{"id": b.id, "name": b.name, "builtin": b.builtin} for b in blocks]
