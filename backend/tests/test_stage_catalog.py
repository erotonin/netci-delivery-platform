"""The stage catalog: pipelines configured from the portal, never a Jenkinsfile."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import stage_catalog as sc
import app.main as main

TEMPLATE = ("checkout", "unit-test", "build", "sbom", "vulnerability-scan", "sign", "publish", "deploy", "health-check")


def catalog(*extra):
    items = {s.id: s for s in sc.BUILTIN_STAGES}
    for item in extra:
        items[item.id] = item
    return items


def lint(status="active"):
    return sc.custom_stage(
        stage_id="lint", name="Lint", script="ci/lint.sh", after_stage="unit-test", created_by="pat", status=status,
        parameters=[{"name": "LEVEL", "default": "basic", "description": "strictness"}],
    )


def test_an_empty_choice_is_the_template():
    assert sc.resolve_pipeline_stages(TEMPLATE, [], catalog()) == TEMPLATE


def test_required_stages_cannot_be_dropped():
    with pytest.raises(sc.StageCatalogError) as exc:
        sc.resolve_pipeline_stages(TEMPLATE, ["checkout", "build", "sign", "publish"], catalog())
    assert exc.value.code == "REQUIRED_STAGE_REMOVED"
    assert "sbom" in exc.value.message and "vulnerability-scan" in exc.value.message


def test_optional_builtins_can_be_dropped_but_order_is_the_templates():
    chosen = ["checkout", "build", "sbom", "vulnerability-scan", "sign", "publish"]
    assert sc.resolve_pipeline_stages(TEMPLATE, chosen, catalog()) == tuple(chosen)
    with pytest.raises(sc.StageCatalogError, match="template's order"):
        sc.resolve_pipeline_stages(TEMPLATE, ["build", "checkout", "sbom", "vulnerability-scan", "sign", "publish"], catalog())


def test_a_custom_stage_lands_right_after_its_anchor():
    chosen = ["checkout", "unit-test", "build", "sbom", "vulnerability-scan", "sign", "publish", "lint"]
    resolved = sc.resolve_pipeline_stages(TEMPLATE, chosen, catalog(lint()))
    assert resolved == ("checkout", "unit-test", "lint", "build", "sbom", "vulnerability-scan", "sign", "publish")
    assert sc.custom_stage_parameters(resolved, catalog(lint())) == [
        {"id": "lint", "name": "Lint", "script": "ci/lint.sh", "after": "unit-test", "env": {"LEVEL": "basic"}}
    ]
    assert sc.custom_stage_parameters(resolved, catalog(lint()), {"lint": {"LEVEL": "strict"}})[0]["env"] == {"LEVEL": "strict"}


def test_a_proposed_stage_is_not_part_of_any_pipeline_until_approved():
    chosen = ["checkout", "unit-test", "build", "sbom", "vulnerability-scan", "sign", "publish", "lint"]
    with pytest.raises(sc.StageCatalogError) as exc:
        sc.resolve_pipeline_stages(TEMPLATE, chosen, catalog(lint(status="proposed")))
    assert exc.value.code == "STAGE_NOT_ACTIVE"


@pytest.mark.parametrize("values, code", [
    ({"lint": {"LEVEL": "strict; rm -rf /"}}, "INVALID_STAGE_PARAMETER"),
    ({"lint": {"LEVEL": "$(id)"}}, "INVALID_STAGE_PARAMETER"),
    ({"lint": {"UNDECLARED": "x"}}, "INVALID_STAGE_PARAMETER"),
    ({"build": {"X": "y"}}, "INVALID_STAGE_PARAMETER"),
])
def test_stage_parameter_values_are_declared_names_with_safe_values(values, code):
    stages = ("checkout", "unit-test", "lint", "build", "sbom", "vulnerability-scan", "sign", "publish")
    with pytest.raises(sc.StageCatalogError) as exc:
        sc.validate_stage_parameters(stages, values, catalog(lint()))
    assert exc.value.code == code
    assert sc.validate_stage_parameters(stages, {"lint": {"LEVEL": "strict"}}, catalog(lint())) == {"lint": {"LEVEL": "strict"}}


def test_a_custom_stage_whose_anchor_is_disabled_is_refused():
    chosen = ["checkout", "build", "sbom", "vulnerability-scan", "sign", "publish", "lint"]
    with pytest.raises(sc.StageCatalogError) as exc:
        sc.resolve_pipeline_stages(TEMPLATE, chosen, catalog(lint()))
    assert exc.value.code == "STAGE_ANCHOR_DISABLED"


def test_unknown_stages_are_refused():
    with pytest.raises(sc.StageCatalogError) as exc:
        sc.resolve_pipeline_stages(TEMPLATE, ["checkout", "build", "sbom", "vulnerability-scan", "sign", "publish", "mystery"], catalog())
    assert exc.value.code == "UNKNOWN_STAGE"


@pytest.mark.parametrize("script", ["../escape.sh", "/etc/x.sh", "a/../b.sh", "run.py", "lint", "ci/x.sh; rm -rf /"])
def test_a_custom_stage_script_is_a_repository_path_never_a_command(script):
    with pytest.raises(sc.StageCatalogError) as exc:
        sc.custom_stage(stage_id="probe", name="x", script=script, after_stage="build", created_by="pat")
    assert exc.value.code == "INVALID_STAGE_SCRIPT"


def test_a_custom_stage_cannot_take_a_builtin_id_or_an_unanchorable_position():
    with pytest.raises(sc.StageCatalogError, match="built-in"):
        sc.custom_stage(stage_id="build", name="x", script="ci/x.sh", after_stage="build", created_by="pat")
    with pytest.raises(sc.StageCatalogError, match="runs after one of"):
        sc.custom_stage(stage_id="post", name="x", script="ci/x.sh", after_stage="deploy", created_by="pat")


# ------------------------------------------------------------------ through the API


# Other test modules `importlib.reload(app.main)`, which rebinds its globals; resolve
# the app and platform at call time so this module never holds a stale instance.
@pytest.fixture(autouse=True)
def reset():
    main.platform.reset()
    main.portal.reset()


class _Client:
    def __getattr__(self, name):
        return getattr(TestClient(main.app), name)


client = _Client()


def test_the_catalog_is_data_and_a_module_chooses_from_it_in_the_portal():
    listed = client.get("/stage-catalog").json()
    assert [s["id"] for s in listed["stages"]] == list(TEMPLATE)
    assert all(s["kind"] == "builtin" for s in listed["stages"])

    registered = client.post("/stage-catalog", json={
        "id": "lint", "name": "Lint", "script": "ci/lint.sh", "afterStage": "unit-test", "description": "shellcheck",
        "parameters": [{"name": "LEVEL", "default": "basic"}],
    })
    assert registered.status_code == 201, registered.text
    assert registered.json()["kind"] == "custom" and registered.json()["position"] == 25
    assert [s["id"] for s in client.get("/stage-catalog").json()["stages"]][:3] == ["checkout", "unit-test", "lint"]
    # With the anonymous test principal there is no second person, so separation of
    # duties is off and the stage is active at once; the two-person rule is covered
    # by test_a_custom_stage_needs_a_second_administrator.
    assert registered.json()["status"] == "active"

    module = client.get("/modules/hello-container").json()
    chosen = client.put("/modules/hello-container/stages", json={
        "stages": ["checkout", "unit-test", "lint", "build", "sbom", "vulnerability-scan", "sign", "publish"],
        "stageParameters": {"lint": {"LEVEL": "strict"}},
    })
    assert chosen.status_code == 200, chosen.text
    assert chosen.json()["stages"] == ["checkout", "unit-test", "lint", "build", "sbom", "vulnerability-scan", "sign", "publish"]
    assert chosen.json()["stageParameters"] == {"lint": {"LEVEL": "strict"}}
    assert client.get("/modules/hello-container/stages").json()["stageParameters"] == {"lint": {"LEVEL": "strict"}}
    rejected = client.put("/modules/hello-container/stages", json={
        "stages": ["checkout", "unit-test", "lint", "build", "sbom", "vulnerability-scan", "sign", "publish"],
        "stageParameters": {"lint": {"LEVEL": "strict && curl evil"}},
    })
    assert rejected.status_code == 422 and rejected.json()["code"] == "INVALID_STAGE_PARAMETER"
    application = main.platform.get_application(__import__("uuid").UUID(module["applicationId"]))
    assert "lint" in application.stages

    # The catalog cannot lose a stage a module runs.
    refused = client.delete("/stage-catalog/lint")
    assert refused.status_code == 409 and refused.json()["code"] == "STAGE_IN_USE"

    # Dropping a required stage is refused with the reason.
    denied = client.put("/modules/hello-container/stages", json={"stages": ["checkout", "build", "sign", "publish"]})
    assert denied.status_code == 422 and denied.json()["code"] == "REQUIRED_STAGE_REMOVED"

    # Built-ins are not removable, even by an admin.
    assert client.delete("/stage-catalog/sbom").status_code == 409


def test_a_run_carries_the_custom_stage_to_jenkins(monkeypatch):
    from app.adapters.ci_launcher import CiLaunchRequest, LaunchedCi

    captured: list[CiLaunchRequest] = []

    class Launcher:
        mode = "recording"

        def launch(self, request):
            captured.append(request)
            return LaunchedCi(controller_id="jenkins-a", external_run_id="job#1", console_url="http://j/1")

    monkeypatch.setattr(main.platform, "ci_launcher", Launcher())
    client.post("/stage-catalog", json={"id": "lint", "name": "Lint", "script": "ci/lint.sh", "afterStage": "unit-test",
                                        "parameters": [{"name": "LEVEL", "default": "basic"}]})
    client.put("/modules/hello-container/stages", json={
        "stages": ["checkout", "unit-test", "lint", "build", "sbom", "vulnerability-scan", "sign", "publish"],
        "stageParameters": {"lint": {"LEVEL": "strict"}},
    })
    run = client.post("/modules/hello-container/pipeline-runs", json={"commitSha": "a" * 40, "branch": "main", "environment": "dev"})
    assert run.status_code == 202, run.text
    assert captured[0].stages == ("checkout", "unit-test", "lint", "build", "sbom", "vulnerability-scan", "sign", "publish")
    assert captured[0].custom_stages == [{"id": "lint", "name": "Lint", "script": "ci/lint.sh", "after": "unit-test", "env": {"LEVEL": "strict"}}]


def test_a_custom_stage_needs_a_second_administrator():
    """A stage is code on every agent; one person must not both propose and approve it."""
    from app.delivery import DeliveryError

    proposed = main.platform.register_custom_stage(
        actor="admin-a", requires_approval=True, stage_id="scan-licenses", name="Licenses",
        script="ci/licenses.sh", after_stage="build",
    )
    assert proposed["status"] == "proposed"
    refused = client.put("/modules/hello-container/stages", json={
        "stages": ["checkout", "unit-test", "build", "scan-licenses", "sbom", "vulnerability-scan", "sign", "publish"],
    })
    assert refused.status_code == 422 and refused.json()["code"] == "STAGE_NOT_ACTIVE"
    with pytest.raises(DeliveryError) as exc:
        main.platform.approve_custom_stage("scan-licenses", actor="admin-a", separation_of_duties=True)
    assert exc.value.code == "SEPARATION_OF_DUTIES"
    approved = main.platform.approve_custom_stage("scan-licenses", actor="admin-b", separation_of_duties=True)
    assert approved["status"] == "active" and approved["approvedBy"] == "admin-b"
    assert client.put("/modules/hello-container/stages", json={
        "stages": ["checkout", "unit-test", "build", "scan-licenses", "sbom", "vulnerability-scan", "sign", "publish"],
    }).status_code == 200
