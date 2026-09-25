"""`POST /release-plans/simulate`: the production-request release algorithm, read-only.

The route must show exactly what `compute_dag_waves` decides for a plan -- the same
waves a production request with these dependencies would get -- and, when it refuses a
plan, the loop that blocks it. It must never invent dependency edges: netCI stores none
for a system, so a system-based simulation is refused rather than drawn as one wave.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

import app.main as main_mod
from app.domain.dag import compute_dag_waves, find_cycle

client = TestClient(main_mod.app)


@pytest.fixture(autouse=True)
def fresh_platform():
    # Other test modules reload app.main, which leaves an import-time client on a stale app.
    global client
    client = TestClient(main_mod.app)
    main_mod.platform.reset()
    yield


CLASSIC = [
    {"moduleId": "db-migration", "dependencies": []},
    {"moduleId": "redis", "dependencies": []},
    {"moduleId": "auth", "dependencies": ["db-migration", "redis"]},
    {"moduleId": "payment", "dependencies": ["db-migration"]},
    {"moduleId": "frontend", "dependencies": ["auth", "payment"]},
    {"moduleId": "api-gateway", "dependencies": ["auth"]},
]

CYCLE = [
    {"moduleId": "a", "dependencies": ["b"]},
    {"moduleId": "b", "dependencies": ["c"]},
    {"moduleId": "c", "dependencies": ["a"]},
]


def _simulate(body: dict):
    return client.post("/release-plans/simulate", json=body)


def _assert_is_cycle(found: list[str], expected: list[str]) -> None:
    # A cycle has no first node: [b, c, a] is the same loop as [a, b, c].
    assert found is not None and len(found) == len(expected)
    start = found.index(expected[0])
    assert found[start:] + found[:start] == expected


# ------------------------------------------------------------------------- the route


def test_classic_graph_releases_in_three_waves():
    response = _simulate({"modules": CLASSIC})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["valid"] is True
    assert body["groupedBy"] == "dependencies"
    assert [set(wave) for wave in body["waves"]] == [
        {"db-migration", "redis"},
        {"auth", "payment"},
        {"frontend", "api-gateway"},
    ]
    assert body["order"] == [module for wave in body["waves"] for module in wave]


def test_waves_are_the_ones_a_production_request_would_get():
    # The route maps compute_dag_waves' result; it must not have its own algorithm.
    plan = compute_dag_waves([dict(m, deploymentOrder=1) for m in CLASSIC])

    body = _simulate({"modules": CLASSIC}).json()

    assert body["waves"] == [wave["moduleIds"] for wave in plan["waves"]]


def test_cycle_is_refused_with_the_loop_named():
    response = _simulate({"modules": CYCLE})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["valid"] is False
    assert body["code"] == "CYCLIC_DEPENDENCY"
    assert body["message"]
    assert "waves" not in body
    _assert_is_cycle(body["cycle"], ["a", "b", "c"])


def test_cycle_downstream_modules_are_not_reported_as_part_of_the_loop():
    # d depends on the cycle, so Kahn's algorithm never releases it either -- but it is
    # not what has to be broken.
    modules = CYCLE + [{"moduleId": "d", "dependencies": ["a"]}]

    body = _simulate({"modules": modules}).json()

    assert body["code"] == "CYCLIC_DEPENDENCY"
    _assert_is_cycle(body["cycle"], ["a", "b", "c"])


def test_self_dependency_is_a_cycle_of_one():
    body = _simulate({"modules": [{"moduleId": "a", "dependencies": ["a"]}]}).json()

    assert body == {
        "valid": False,
        "code": "CYCLIC_DEPENDENCY",
        "message": body["message"],
        "cycle": ["a"],
    }


def test_unknown_dependency_is_refused_without_a_cycle():
    body = _simulate({"modules": [{"moduleId": "auth", "dependencies": ["db-migration"]}]}).json()

    assert body["valid"] is False
    assert body["code"] == "INVALID_DEPENDENCY"
    assert "db-migration" in body["message"]
    assert body["cycle"] is None


def test_duplicate_module_is_refused():
    body = _simulate({"modules": [{"moduleId": "auth"}, {"moduleId": "auth"}]}).json()

    assert body["valid"] is False
    assert body["code"] == "DUPLICATE_MODULE"
    assert body["cycle"] is None


def test_no_dependencies_says_the_waves_follow_deployment_order():
    body = _simulate(
        {"modules": [{"moduleId": "b", "deploymentOrder": 2}, {"moduleId": "a", "deploymentOrder": 1}]}
    ).json()

    assert body["valid"] is True
    assert body["groupedBy"] == "deploymentOrder"
    assert body["waves"] == [["a"], ["b"]]


def test_simulation_stores_nothing():
    before = client.get("/production-requests").json()

    _simulate({"modules": CLASSIC})

    assert client.get("/production-requests").json() == before


# --------------------------------------------------------------------------- limits


def test_more_than_200_modules_is_rejected():
    modules = [{"moduleId": f"m-{i}"} for i in range(201)]

    response = _simulate({"modules": modules})

    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"


def test_200_modules_is_accepted():
    modules = [{"moduleId": f"m-{i}", "dependencies": [f"m-{i - 1}"] if i else []} for i in range(200)]

    body = _simulate({"modules": modules}).json()

    assert body["valid"] is True
    assert len(body["waves"]) == 200


def test_more_than_50_dependencies_is_rejected():
    deps = [f"d-{i}" for i in range(51)]
    modules = [{"moduleId": d} for d in deps] + [{"moduleId": "top", "dependencies": deps}]

    assert _simulate({"modules": modules}).status_code == 422


@pytest.mark.parametrize(
    "body",
    [
        {"modules": []},
        {},
        {"modules": [{"moduleId": "a"}], "systemId": "sys-one"},
        {"modules": [{"moduleId": "Upper"}]},
        {"modules": [{"moduleId": "a", "dependencies": ["Not A Slug"]}]},
        {"modules": [{"moduleId": "a", "version": "1.0.0"}]},
        {"modules": [{"moduleId": "a"}], "requestedBy": "someone"},
    ],
)
def test_malformed_bodies_are_422(body):
    response = _simulate(body)

    assert response.status_code == 422, response.text
    assert response.json()["code"] == "VALIDATION_ERROR"


# --------------------------------------------------------------------------- systems


def test_system_is_refused_because_netci_stores_no_dependencies_for_it():
    system_id = f"sys-{uuid4().hex[:6]}"
    assert client.post(
        "/systems", json={"id": system_id, "unit": "Release plans", "description": "test system"}
    ).status_code == 201

    response = _simulate({"systemId": system_id})

    assert response.status_code == 422
    assert response.json()["code"] == "SYSTEM_HAS_NO_DEPENDENCY_DATA"


def test_unknown_system_is_404():
    response = _simulate({"systemId": "sys-does-not-exist"})

    assert response.status_code == 404
    assert response.json()["code"] == "SYSTEM_NOT_FOUND"


# ------------------------------------------------------------------------ find_cycle


def test_find_cycle_none_for_a_dag():
    assert find_cycle(CLASSIC) is None


def test_find_cycle_names_the_loop_in_dependency_order():
    assert find_cycle(CYCLE) == ["a", "b", "c"]


def test_find_cycle_is_deterministic_and_follows_input_order():
    # Two disjoint loops: the one reached first from the first module is reported.
    modules = [
        {"moduleId": "x", "dependencies": ["y"]},
        {"moduleId": "y", "dependencies": ["x"]},
        {"moduleId": "a", "dependencies": ["b"]},
        {"moduleId": "b", "dependencies": ["a"]},
    ]
    assert find_cycle(modules) == ["x", "y"]
    assert find_cycle(modules) == ["x", "y"]


def test_find_cycle_excludes_the_path_into_the_loop():
    modules = [
        {"moduleId": "entry", "dependencies": ["a"]},
        {"moduleId": "a", "dependencies": ["b"]},
        {"moduleId": "b", "dependencies": ["a"]},
    ]
    assert find_cycle(modules) == ["a", "b"]


def test_find_cycle_self_loop():
    assert find_cycle([{"moduleId": "a", "dependencies": ["a"]}]) == ["a"]


def test_find_cycle_skips_unknown_dependencies_and_empty_input():
    assert find_cycle([{"moduleId": "a", "dependencies": ["ghost"]}]) is None
    assert find_cycle([]) is None


def test_find_cycle_handles_a_long_chain_without_recursion():
    n = 5000
    modules = [{"moduleId": f"m{i}", "dependencies": [f"m{(i + 1) % n}"]} for i in range(n)]
    assert find_cycle(modules) == [f"m{i}" for i in range(n)]


def test_find_cycle_does_not_rescan_finished_nodes():
    # A diamond reaches `base` twice; the second visit must not be read as a back edge.
    modules = [
        {"moduleId": "top", "dependencies": ["left", "right"]},
        {"moduleId": "left", "dependencies": ["base"]},
        {"moduleId": "right", "dependencies": ["base"]},
        {"moduleId": "base"},
    ]
    assert find_cycle(modules) is None
