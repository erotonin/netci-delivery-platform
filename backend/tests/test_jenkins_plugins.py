"""netCI decides the Jenkins controller's plugins (ADR-059).

toolchain/versions.yaml pins every plugin, dependencies included. A controller running
anything else -- another version, a plugin missing, one added by hand -- does not take
builds, and neither does one whose plugin list cannot be read: unreadable is not "matches".
"""

from __future__ import annotations

import copy
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app import toolchain
from app.adapters.ci_launcher import CiLaunchError, CiLaunchRequest, JenkinsCiLauncher
from app.adapters.interfaces import JenkinsRun
from app.adapters.jenkins_http import JenkinsHttpAdapter, JenkinsHttpConfig, JenkinsHttpError
from app.adapters.jenkins_router import JenkinsController, JenkinsRouter

DECLARED = toolchain.declared()
PLUGINS = dict(DECLARED["jenkins"]["plugins"])


def test_the_declaration_pins_every_plugin_and_the_base_by_digest():
    assert "@sha256:" in DECLARED["jenkins"]["controller"]["base"]
    assert set(DECLARED["jenkins"]["requires"]) <= set(PLUGINS)
    for broken in (
        lambda d: d.pop("jenkins"),
        lambda d: d["jenkins"]["controller"].update(base="jenkins/jenkins:lts"),
        lambda d: d["jenkins"].update(plugins={}),
        lambda d: d["jenkins"]["plugins"].update({"git": ""}),
        lambda d: d["jenkins"].update(requires=["not-pinned"]),
    ):
        spec = copy.deepcopy(DECLARED)
        broken(spec)
        with pytest.raises(toolchain.ToolchainValidationError):
            toolchain.validate_toolchain_dict(spec)


def test_drift_names_each_plugin_and_how_it_differs():
    observed = dict(PLUGINS)
    observed["git"] = "0.0.1"
    del observed["matrix-auth"]
    observed["script-console-plus"] = "1.0"
    drift = {d["plugin"]: d for d in toolchain.compare_plugins(observed, DECLARED)}
    assert drift["git"]["kind"] == "version" and drift["git"]["observed"] == "0.0.1"
    assert drift["matrix-auth"]["kind"] == "missing"
    assert drift["script-console-plus"]["kind"] == "undeclared"
    assert len(drift) == 3
    assert toolchain.compare_plugins(dict(PLUGINS), DECLARED) == []


def test_nothing_observed_is_every_plugin_missing_not_a_match():
    assert len(toolchain.compare_plugins({}, DECLARED)) == len(PLUGINS)


# ------------------------------------------------------------------ the adapter


def _adapter(routes):
    adapter = JenkinsHttpAdapter(JenkinsHttpConfig(base_url="http://jenkins", username="u", api_token="t"))

    def fake(method, path, **_):
        answer = routes[(method, path)]
        return answer

    adapter._request = fake  # type: ignore[method-assign]
    return adapter


PLUGIN_PATH = "/pluginManager/api/json?depth=1&tree=plugins%5BshortName,version,active%5D"


def test_the_adapter_reads_active_plugins_only():
    import json

    body = json.dumps({"plugins": [
        {"shortName": "git", "version": "5.10.1", "active": True},
        {"shortName": "old", "version": "1.0", "active": False},
    ]}).encode()
    assert _adapter({("GET", PLUGIN_PATH): (200, {}, body)}).installed_plugins() == {"git": "5.10.1"}


@pytest.mark.parametrize("answer", [(403, {}, b"denied"), (200, {}, b'{"plugins": []}')])
def test_an_unreadable_or_empty_plugin_list_raises(answer):
    with pytest.raises(JenkinsHttpError):
        _adapter({("GET", PLUGIN_PATH): answer}).installed_plugins()


# ------------------------------------------------------------------ the launch gate


class Controller:
    def __init__(self, plugins):
        self.plugins = plugins
        self.triggered = []

    def health_check(self):
        return True

    def installed_plugins(self):
        if isinstance(self.plugins, Exception):
            raise self.plugins
        return self.plugins

    def create_or_update_job(self, application_id, template_id):
        return "netci-job"

    def trigger_ci_run(self, job_name, request, callback_token):
        self.triggered.append(request.pipeline_run_id)
        return JenkinsRun(run_id="netci-job#1", status="QUEUED", console_url=None)


def _launcher(**adapters):
    controllers = [JenkinsController(controller_id=name, executors_total=4) for name in adapters]
    return JenkinsCiLauncher(JenkinsRouter(controllers), adapters)


def _request():
    return CiLaunchRequest(
        application_id=uuid4(), application_name="payments-api", repository_url="https://git.example/p.git",
        pipeline_template="container-ci-cd-v1", runtime="docker", stages=("checkout", "build"),
        pipeline_run_id=uuid4(), commit_sha="a" * 40, branch="main", environment="dev",
        correlation_id="c", parameters={},
    )


def _drifted():
    plugins = dict(PLUGINS)
    plugins["credentials-binding"] = "719.v80e905ef14eb_"
    return plugins


def test_a_controller_with_undeclared_plugins_takes_no_build():
    drifted = Controller(_drifted())
    with pytest.raises(CiLaunchError) as exc:
        _launcher(a=drifted).launch(_request())
    assert "JENKINS_PLUGIN_DRIFT" in str(exc.value.__cause__ or exc.value) or "JENKINS_PLUGIN_DRIFT" in str(exc.value)
    assert "credentials-binding" in str(exc.value)
    assert drifted.triggered == []


def test_a_build_goes_to_the_controller_that_matches():
    drifted, matching = Controller(_drifted()), Controller(dict(PLUGINS))
    launched = _launcher(a=drifted, b=matching).launch(_request())
    assert launched.controller_id == "b" and drifted.triggered == [] and len(matching.triggered) == 1


def test_an_unreadable_plugin_list_refuses_the_controller():
    blind = Controller(JenkinsHttpError(403, "plugin list returned 403"))
    with pytest.raises(CiLaunchError) as exc:
        _launcher(a=blind).launch(_request())
    assert "plugin set unreadable" in str(exc.value) and blind.triggered == []


def test_warn_mode_records_and_allows(monkeypatch, caplog):
    monkeypatch.setenv("NETCI_TOOLCHAIN_ENFORCE", "warn")
    drifted = Controller(_drifted())
    assert _launcher(a=drifted).launch(_request()).controller_id == "a"
    assert "JENKINS_PLUGIN_DRIFT" in caplog.text


def test_the_toolchain_page_shows_each_controllers_plugin_drift(monkeypatch):
    launcher = _launcher(a=Controller(_drifted()), b=Controller(JenkinsHttpError(403, "denied")))
    monkeypatch.setattr(main.platform, "ci_launcher", launcher)
    report = TestClient(main.app).get("/toolchain").json()["jenkins"]
    by_id = {c["controllerId"]: c for c in report["controllers"]}
    assert [d["plugin"] for d in by_id["a"]["drift"]] == ["credentials-binding"]
    assert by_id["b"]["readable"] is False and "denied" in by_id["b"]["error"]
    assert report["controller"]["tag"] == DECLARED["jenkins"]["controller"]["tag"] and report["enforced"] is True
