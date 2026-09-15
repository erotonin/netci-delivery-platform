"""Golden Path Pipeline Templates domain logic."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from ..store.records import CatalogTemplateRecord
from ..store.session import PlatformSession


SEMVER_REGEX = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?$")


class TemplateValidationError(ValueError):
    """Raised when template validation or instantiation fails."""


@dataclass(frozen=True)
class InstantiatedTemplate:
    template_id: str
    version: str
    application_name: str
    owning_team: str
    runtime: str
    stages: list[dict[str, Any]]
    pipeline_config: dict[str, Any]
    deployment_config: dict[str, Any]


class PipelineTemplateEngine:
    """Manages versioned golden path pipeline templates and their parameter-driven instantiation."""

    def __init__(self, session: PlatformSession) -> None:
        self._session = session

    def register_template(
        self,
        *,
        template_id: str,
        version: str,
        name: str,
        description: str = "",
        category: str = "backend",
        parameters_schema: dict[str, Any] | None = None,
        pipeline_definition: dict[str, Any] | None = None,
        is_deprecated: bool = False,
    ) -> CatalogTemplateRecord:
        template_id = template_id.strip()
        if not template_id:
            raise TemplateValidationError("template_id cannot be empty")
        version = version.strip()
        if not SEMVER_REGEX.match(version):
            raise TemplateValidationError(
                f"invalid version '{version}', must follow semantic versioning (e.g. v1.0.0)"
            )
        if not name.strip():
            raise TemplateValidationError("template name cannot be empty")

        now = datetime.now(timezone.utc)
        record = CatalogTemplateRecord(
            id=template_id,
            version=version,
            name=name.strip(),
            description=description.strip(),
            category=category.strip(),
            parameters_schema=dict(parameters_schema or {}),
            pipeline_definition=dict(pipeline_definition or {}),
            is_deprecated=is_deprecated,
            created_at=now,
            updated_at=now,
        )
        self._session.insert_catalog_template(record)
        return record

    def validate_parameters(
        self, schema: dict[str, Any], parameters: dict[str, Any]
    ) -> dict[str, Any]:
        """Validate provided parameters against the template's JSON schema (simplified validator)."""
        properties = schema.get("properties", {})
        required = schema.get("required", [])

        # Check required fields
        for req_key in required:
            if req_key not in parameters:
                raise TemplateValidationError(f"missing required parameter: '{req_key}'")

        validated: dict[str, Any] = {}
        for key, prop in properties.items():
            val = parameters.get(key)
            if val is None:
                if "default" in prop:
                    val = prop["default"]
                else:
                    continue

            expected_type = prop.get("type")
            if expected_type == "string" and not isinstance(val, str):
                raise TemplateValidationError(f"parameter '{key}' must be a string")
            elif expected_type == "integer" and not isinstance(val, int):
                raise TemplateValidationError(f"parameter '{key}' must be an integer")
            elif expected_type == "boolean" and not isinstance(val, bool):
                raise TemplateValidationError(f"parameter '{key}' must be a boolean")

            if "enum" in prop and val not in prop["enum"]:
                raise TemplateValidationError(
                    f"parameter '{key}' value '{val}' not in allowed enum: {prop['enum']}"
                )

            validated[key] = val

        return validated

    def instantiate(
        self,
        *,
        template_id: str,
        version: str | None = None,
        application_name: str,
        owning_team: str,
        parameters: dict[str, Any] | None = None,
    ) -> InstantiatedTemplate:
        template = self._session.catalog_template(template_id, version)
        if not template:
            ver_str = f" version {version}" if version else ""
            raise TemplateValidationError(f"template '{template_id}'{ver_str} not found")

        if template.is_deprecated:
            raise TemplateValidationError(
                f"template '{template_id}' version '{template.version}' is deprecated"
            )

        user_params = dict(parameters or {})
        validated_params = self.validate_parameters(template.parameters_schema, user_params)

        pipeline_def = template.pipeline_definition
        runtime = pipeline_def.get("runtime", "docker")
        raw_stages = pipeline_def.get("stages", ["build", "test", "deploy"])

        # Interpolate parameters into stages
        instantiated_stages: list[dict[str, Any]] = []
        for stg in raw_stages:
            if isinstance(stg, str):
                stage_obj = {
                    "name": stg,
                    "stage_id": stg,
                    "command": f"make {stg}",
                    "timeout_seconds": 600,
                }
            elif isinstance(stg, dict):
                stage_obj = dict(stg)
                # replace {{ param }} in commands/envs
                if "command" in stage_obj and isinstance(stage_obj["command"], str):
                    cmd = stage_obj["command"]
                    for pk, pv in validated_params.items():
                        cmd = cmd.replace(f"{{{{ {pk} }}}}", str(pv))
                        cmd = cmd.replace(f"{{{{{pk}}}}}", str(pv))
                    stage_obj["command"] = cmd
            else:
                continue
            instantiated_stages.append(stage_obj)

        pipeline_config = {
            "template_id": template.id,
            "template_version": template.version,
            "parameters": validated_params,
            "stages": instantiated_stages,
        }

        deployment_config = {
            "runtime": runtime,
            "target_environment": validated_params.get("environment", "dev"),
            "port": validated_params.get("port", 8000),
            "replicas": validated_params.get("replicas", 1),
        }

        return InstantiatedTemplate(
            template_id=template.id,
            version=template.version,
            application_name=application_name,
            owning_team=owning_team,
            runtime=runtime,
            stages=instantiated_stages,
            pipeline_config=pipeline_config,
            deployment_config=deployment_config,
        )


def seed_builtin_templates(session: PlatformSession) -> None:
    """Ensure standard golden path templates exist in the catalog."""
    engine = PipelineTemplateEngine(session)

    # 1. FastAPI Golden Path
    if not session.catalog_template("fastapi-service", "v1.0.0"):
        engine.register_template(
            template_id="fastapi-service",
            version="v1.0.0",
            name="FastAPI Service Golden Path",
            description="Production-grade Python 3.12 FastAPI microservice with automated linting, pytest, Trivy scanning, and container packaging",
            category="backend",
            parameters_schema={
                "type": "object",
                "required": ["port"],
                "properties": {
                    "python_version": {"type": "string", "default": "3.12", "enum": ["3.11", "3.12"]},
                    "port": {"type": "integer", "default": 8000},
                    "enable_telemetry": {"type": "boolean", "default": True},
                },
            },
            pipeline_definition={
                "runtime": "docker",
                "stages": [
                    {"name": "lint", "command": "ruff check .", "stage_id": "lint"},
                    {"name": "test", "command": "pytest --cov", "stage_id": "test"},
                    {"name": "security-scan", "command": "trivy fs --exit-code 1 .", "stage_id": "security-scan"},
                    {"name": "build", "command": "docker build -t app .", "stage_id": "build"},
                ],
            },
        )

    # 2. Go Microservice Golden Path
    if not session.catalog_template("go-microservice", "v1.0.0"):
        engine.register_template(
            template_id="go-microservice",
            version="v1.0.0",
            name="Go Microservice Golden Path",
            description="High-performance Go 1.22 microservice with golangci-lint, unit tests, race detection, and scratch container build",
            category="backend",
            parameters_schema={
                "type": "object",
                "required": ["port"],
                "properties": {
                    "go_version": {"type": "string", "default": "1.22"},
                    "port": {"type": "integer", "default": 8080},
                },
            },
            pipeline_definition={
                "runtime": "docker",
                "stages": [
                    {"name": "lint", "command": "golangci-lint run", "stage_id": "lint"},
                    {"name": "test", "command": "go test -race ./...", "stage_id": "test"},
                    {"name": "build", "command": "CGO_ENABLED=0 go build -o app .", "stage_id": "build"},
                ],
            },
        )

    # 3. React SPA Golden Path
    if not session.catalog_template("react-spa", "v1.0.0"):
        engine.register_template(
            template_id="react-spa",
            version="v1.0.0",
            name="React Single Page Application",
            description="Modern TypeScript React application with Vitest, ESLint, Vite build, and static bundle delivery",
            category="frontend",
            parameters_schema={
                "type": "object",
                "properties": {
                    "node_version": {"type": "string", "default": "20"},
                    "spa_routing": {"type": "boolean", "default": True},
                },
            },
            pipeline_definition={
                "runtime": "docker",
                "stages": [
                    {"name": "lint", "command": "npm run lint", "stage_id": "lint"},
                    {"name": "test", "command": "npm test -- --run", "stage_id": "test"},
                    {"name": "build", "command": "npm run build", "stage_id": "build"},
                ],
            },
        )
