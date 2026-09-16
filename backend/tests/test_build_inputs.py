"""The boundary between what a caller may ask for and what the server decides.

`parameters` used to be an open map passed through the domain into the CD orchestrator,
where runtime adapters read `target_hosts`, `kubeconfig_ref` and `artifact_url` out of it.
A browser could therefore pick the machines a deployment touched by adding a JSON key.
Every refusal below is a thing that used to be accepted.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.build_inputs import BuildInputError, validate_build_inputs
from app.main import app

client = TestClient(app)


def setup_function():
    main.platform.reset()
    main.portal.reset()


# ------------------------------------------------------------------- unit level


@pytest.mark.parametrize(
    "key",
    [
        "target_hosts", "targetHosts", "hosts", "namespace", "target_namespace",
        "kubeconfig_ref", "kubeconfigRef", "credentialsId", "ssh_private_key",
        "artifact_url", "artifact_ref", "artifact_digest", "image_repository",
        "playbook", "health_command", "rollback_strategy", "runtime_health_verified",
        "deployment_tasks", "task_settings", "actor", "owner_team", "app_name",
        "release_track", "releaseTrack", "canary_weight",
    ],
)
def test_a_deployment_controlled_key_is_refused_not_dropped(key):
    """Dropping it silently would teach the caller the override worked."""

    with pytest.raises(BuildInputError) as failure:
        validate_build_inputs({key: "anything"})

    assert failure.value.code == "DEPLOYMENT_PARAMETER_NOT_ACCEPTED"
    assert key in failure.value.message


def test_case_and_dashes_do_not_get_a_key_past_the_boundary():
    for spelling in ("TARGET-HOSTS", "Target_Hosts", "kube-config-ref".replace("-", "_")):
        with pytest.raises(BuildInputError):
            validate_build_inputs({spelling: "x"})


def test_an_unknown_key_is_refused():
    with pytest.raises(BuildInputError) as failure:
        validate_build_inputs({"somethingNew": 1})
    assert failure.value.code == "BUILD_INPUT_NOT_ALLOWED"


def test_allowed_build_inputs_pass_through_unchanged():
    supplied = {"buildProfile": "release", "skipTests": False, "logLevel": "debug",
                "buildArgs": {"VERSION": "1.2.3", "DEBUG": True}}

    assert validate_build_inputs(supplied) == supplied


def test_a_deployment_key_nested_inside_an_allowed_object_is_still_refused():
    with pytest.raises(BuildInputError) as failure:
        validate_build_inputs({"buildArgs": {"kubeconfig_ref": "team-prod"}})
    assert failure.value.code == "DEPLOYMENT_PARAMETER_NOT_ACCEPTED"


def test_nesting_is_bounded():
    with pytest.raises(BuildInputError) as failure:
        validate_build_inputs({"buildArgs": {"deep": {"deeper": 1}}})
    assert failure.value.code == "BUILD_INPUT_TOO_DEEP"


def test_lists_are_not_accepted():
    with pytest.raises(BuildInputError) as failure:
        validate_build_inputs({"buildProfile": ["a", "b"]})
    assert failure.value.code == "BUILD_INPUT_TYPE_NOT_ALLOWED"


def test_size_is_bounded():
    with pytest.raises(BuildInputError) as failure:
        validate_build_inputs({"notes": "x" * 5000})
    assert failure.value.code == "BUILD_INPUT_TOO_LARGE"

    with pytest.raises(BuildInputError) as many:
        validate_build_inputs({f"k{index}": 1 for index in range(64)})
    assert many.value.code == "BUILD_INPUT_TOO_MANY"


def test_no_parameters_is_the_normal_case():
    assert validate_build_inputs(None) == {}
    assert validate_build_inputs({}) == {}


# -------------------------------------------------------------------- API level


def module_payload(name: str) -> dict:
    return {
        "name": name, "displayName": name,
        "repositoryUrl": f"https://github.com/example/{name}",
        "pipelineTemplate": "container-ci-cd-v1", "runtime": "docker",
        "moduleType": "Backend", "description": "probe",
        "deploymentEnvironments": [
            {"displayName": "Development", "environment": "dev", "runtime": "docker",
             "servers": ["registered-host"]},
        ],
    }


def onboarded_module(name: str = "input-mod") -> str:
    client.post("/systems", json={"id": "input-sys", "unit": "Platform", "description": "inputs"})
    created = client.post("/systems/input-sys/modules", json=module_payload(name))
    assert created.status_code == 201, created.text
    return name


def test_the_api_refuses_a_browser_supplied_deployment_target():
    module_id = onboarded_module()

    refused = client.post(f"/modules/{module_id}/pipeline-runs", json={
        "commitSha": "abc1234", "branch": "main", "environment": "dev",
        "parameters": {"target_hosts": ["attacker-controlled-host"]},
    })

    assert refused.status_code == 422
    assert refused.json()["code"] == "DEPLOYMENT_PARAMETER_NOT_ACCEPTED"
    assert "target_hosts" in refused.json()["message"]


def test_the_server_managed_target_wins_over_anything_a_caller_sends():
    """The registered host is what a run gets, even when the caller sent build inputs."""

    module_id = onboarded_module("managed-mod")

    started = client.post(f"/modules/{module_id}/pipeline-runs", json={
        "commitSha": "abc1234", "branch": "main", "environment": "dev",
        "parameters": {"buildProfile": "release"},
    })

    assert started.status_code == 202
    parameters = started.json()["parameters"]
    assert parameters["target_hosts"] == ["registered-host"]
    assert parameters["app_name"] == module_id
    assert parameters["buildProfile"] == "release"


@pytest.mark.parametrize("branch", [
    "main; rm -rf /", "../../etc/passwd", "main$(whoami)", "main`id`",
    "main\nrm -rf /", "main|cat /etc/shadow", "feature branch",
])
def test_a_branch_that_could_reach_a_shell_or_escape_a_path_is_refused(branch):
    module_id = onboarded_module("branch-mod")

    refused = client.post(f"/modules/{module_id}/pipeline-runs", json={
        "commitSha": "abc1234", "branch": branch, "environment": "dev", "parameters": {},
    })

    assert refused.status_code == 422


@pytest.mark.parametrize("commit", ["abc 1234", "abc;1234", "abc$(id)", "../../abcd123"])
def test_a_commit_that_could_reach_a_shell_is_refused(commit):
    module_id = onboarded_module("commit-mod")

    refused = client.post(f"/modules/{module_id}/pipeline-runs", json={
        "commitSha": commit, "branch": "main", "environment": "dev", "parameters": {},
    })

    assert refused.status_code == 422


def test_the_low_level_application_endpoint_is_not_open_to_developers(monkeypatch):
    """It skips the Portal lookup that binds a run to its registered target.

    With authentication off every caller holds every role, so this asserts the intent at
    the dependency rather than through a token: the route must not be reachable with only
    the developer role.
    """

    from app.main import LowLevelPipelineAccess, PipelineStartAccess
    from app.policy.rules import Role

    low_level_roles = LowLevelPipelineAccess.dependency.__closure__[0].cell_contents
    module_roles = PipelineStartAccess.dependency.__closure__[0].cell_contents

    assert Role.DEVELOPER not in low_level_roles
    assert Role.REVIEWER not in low_level_roles
    assert low_level_roles == frozenset({Role.PLATFORM_ADMIN, Role.PIPELINE})
    assert Role.DEVELOPER in module_roles
