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


#: Routes deliberately absent from `api/openapi.yaml`, each with the reason. Adding a
#: name here is a decision someone has to write down; it is not something a route can do
#: to itself by setting `include_in_schema=False`.
UNPUBLISHED_ROUTES = {
    ("get", "/metrics"): "Prometheus scrape: text/plain exposition, not part of the API contract",
}


def test_every_implemented_route_is_in_the_contract():
    """netCI's rule is that OpenAPI is updated with the implementation, never after.

    Nothing enforced it, so the rule was documentation -- the same shape of defect as an
    authorization check that is written and never invoked. One route (`GET /metrics`) had
    already drifted out of the contract before this test existed.
    """

    contract = load_contract()
    published = {
        (method.lower(), path)
        for path, operations in (contract.get("paths") or {}).items()
        for method in operations
        if method.lower() in {"get", "post", "put", "patch", "delete"}
    }

    implemented = set()
    for route in app.routes:
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None)
        if not path or not methods:
            continue
        for method in methods:
            if method.lower() in {"get", "post", "put", "patch", "delete"}:
                implemented.add((method.lower(), path))

    # FastAPI's own docs endpoints are not netCI's API.
    implemented -= {("get", "/openapi.json"), ("get", "/docs"), ("get", "/redoc"),
                    ("get", "/docs/oauth2-redirect")}

    undocumented = sorted(implemented - published - set(UNPUBLISHED_ROUTES))
    assert not undocumented, (
        "implemented but missing from api/openapi.yaml: "
        + ", ".join(f"{m.upper()} {p}" for m, p in undocumented)
    )

    phantom = sorted(published - implemented)
    assert not phantom, (
        "in api/openapi.yaml but not implemented: "
        + ", ".join(f"{m.upper()} {p}" for m, p in phantom)
    )

    stale_exemptions = sorted(set(UNPUBLISHED_ROUTES) - implemented)
    assert not stale_exemptions, f"exemption for a route that no longer exists: {stale_exemptions}"
