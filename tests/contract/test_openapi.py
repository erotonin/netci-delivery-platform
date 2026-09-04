from pathlib import Path

import yaml
from app.main import app


ROOT = Path(__file__).resolve().parents[2]


def load_contract() -> dict[str, object]:
    return yaml.safe_load((ROOT / "api/openapi.yaml").read_text(encoding="utf-8"))


def collect_local_refs(value: object) -> list[str]:
    if isinstance(value, dict):
        refs = [item for key, item in value.items() if key == "$ref" and isinstance(item, str)]
        return refs + [ref for item in value.values() for ref in collect_local_refs(item)]
    if isinstance(value, list):
        return [ref for item in value for ref in collect_local_refs(item)]
    return []


def resolve_local_ref(contract: dict[str, object], reference: str) -> object:
    value: object = contract
    for part in reference.removeprefix("#/").split("/"):
        if not isinstance(value, dict) or part not in value:
            raise KeyError(reference)
        value = value[part]
    return value


def test_every_local_openapi_reference_resolves():
    contract = load_contract()
    missing = []
    for reference in collect_local_refs(contract):
        if not reference.startswith("#/"):
            continue
        try:
            resolve_local_ref(contract, reference)
        except KeyError:
            missing.append(reference)

    assert sorted(set(missing)) == []


def test_machine_callbacks_use_the_documented_bearer_scheme():
    contract = load_contract()
    scheme = contract["components"]["securitySchemes"]["PipelineBearer"]
    assert scheme == {"type": "http", "scheme": "bearer", "bearerFormat": "API key"}

    for path in (
        "/modules/{moduleId}/versions/{tag}/ci-report",
        "/pipeline-runs/{pipelineRunId}/ci-result",
        "/pipeline-runs/{pipelineRunId}/security-evidence",
        "/deployments/{deploymentId}/result",
    ):
        assert contract["paths"][path]["post"]["security"] == [{"PipelineBearer": []}]


def test_production_request_ids_are_not_incorrectly_constrained_to_uuid():
    schema = load_contract()["components"]["parameters"]["ProductionRequestId"]["schema"]
    assert schema == {"type": "string", "minLength": 1}


def test_portal_module_contract_requires_bounded_deployment_environments():
    schemas = load_contract()["components"]["schemas"]
    module = schemas["ModuleCreate"]
    environments = module["properties"]["deploymentEnvironments"]

    assert "deploymentEnvironments" in module["required"]
    assert environments == {
        "type": "array",
        "minItems": 1,
        "maxItems": 3,
        "items": {"$ref": "#/components/schemas/DeploymentEnvironmentConfig"},
    }
    assert schemas["DeploymentEnvironmentConfig"]["additionalProperties"] is False


def test_system_owner_is_the_authenticated_actor_not_a_body_field():
    schema = load_contract()["paths"]["/systems"]["post"]["requestBody"]["content"]["application/json"]["schema"]

    assert schema["additionalProperties"] is False
    assert set(schema["properties"]) == {"id", "unit", "description"}
    assert "owner" not in schema["properties"]


def test_checked_in_contract_covers_every_live_http_operation():
    contract = load_contract()
    http_methods = {"get", "post", "put", "patch", "delete"}
    documented = {
        (method, path)
        for path, path_item in contract["paths"].items()
        for method in path_item
        if method in http_methods
    }
    live = {
        (method, path)
        for path, path_item in app.openapi()["paths"].items()
        for method in path_item
        if method in http_methods
    }

    assert documented == live
