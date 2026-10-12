# backend/tests/test_dag.py
import pytest
from backend.app.domain.dag import DagValidationError, compute_dag_waves


def test_empty_modules_fails():
    with pytest.raises(DagValidationError) as exc:
        compute_dag_waves([])
    assert exc.value.code == "EMPTY_MODULES"


def test_duplicate_module_fails():
    modules = [
        {"moduleId": "mod-a", "version": "1.0.0"},
        {"moduleId": "mod-a", "version": "1.0.1"},
    ]
    with pytest.raises(DagValidationError) as exc:
        compute_dag_waves(modules)
    assert exc.value.code == "DUPLICATE_MODULE"


def test_self_dependency_fails():
    modules = [
        {"moduleId": "mod-a", "dependencies": ["mod-a"]},
    ]
    with pytest.raises(DagValidationError) as exc:
        compute_dag_waves(modules)
    assert exc.value.code == "CYCLIC_DEPENDENCY"


def test_unknown_dependency_fails():
    modules = [
        {"moduleId": "mod-a", "dependencies": ["mod-ghost"]},
    ]
    with pytest.raises(DagValidationError) as exc:
        compute_dag_waves(modules)
    assert exc.value.code == "INVALID_DEPENDENCY"


def test_linear_dependency():
    modules = [
        {"moduleId": "frontend", "dependencies": ["backend"]},
        {"moduleId": "backend", "dependencies": ["database"]},
        {"moduleId": "database", "dependencies": []},
    ]
    plan = compute_dag_waves(modules)
    assert plan["totalModules"] == 3
    assert plan["totalWaves"] == 3
    assert plan["waves"][0]["moduleIds"] == ["database"]
    assert plan["waves"][1]["moduleIds"] == ["backend"]
    assert plan["waves"][2]["moduleIds"] == ["frontend"]


def test_diamond_dependency():
    #      base
    #     /    \
    #   auth   billing
    #     \    /
    #     portal
    modules = [
        {"moduleId": "portal", "dependencies": ["auth", "billing"]},
        {"moduleId": "auth", "dependencies": ["base"]},
        {"moduleId": "billing", "dependencies": ["base"]},
        {"moduleId": "base", "dependencies": []},
    ]
    plan = compute_dag_waves(modules)
    assert plan["totalWaves"] == 3
    assert plan["waves"][0]["moduleIds"] == ["base"]
    assert sorted(plan["waves"][1]["moduleIds"]) == ["auth", "billing"]
    assert plan["waves"][2]["moduleIds"] == ["portal"]


def test_cycle_detection():
    # A -> B -> C -> A
    modules = [
        {"moduleId": "mod-a", "dependencies": ["mod-c"]},
        {"moduleId": "mod-b", "dependencies": ["mod-a"]},
        {"moduleId": "mod-c", "dependencies": ["mod-b"]},
    ]
    with pytest.raises(DagValidationError) as exc:
        compute_dag_waves(modules)
    assert exc.value.code == "CYCLIC_DEPENDENCY"


def test_fallback_to_deployment_order():
    modules = [
        {"moduleId": "mod-1", "deploymentOrder": 1},
        {"moduleId": "mod-2", "deploymentOrder": 1},
        {"moduleId": "mod-3", "deploymentOrder": 2},
    ]
    plan = compute_dag_waves(modules)
    assert plan["totalWaves"] == 2
    assert sorted(plan["waves"][0]["moduleIds"]) == ["mod-1", "mod-2"]
    assert plan["waves"][1]["moduleIds"] == ["mod-3"]
