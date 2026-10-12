"""scripts/jenkins_preflight.py against a fake controller.

The preflight is what a company runs before pointing netCI at its Jenkins, so what it
leaves out becomes the first failed build: `readJSON` and `cleanWs` come from plugins it
did not check, a folder-only Job/Create grant looked like no permission at all, and an
unverifiable library ref was reported inside a PASS line.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[2] / "scripts" / "jenkins_preflight.py"
_spec = importlib.util.spec_from_file_location("jenkins_preflight", _PATH)
preflight = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
_spec.loader.exec_module(preflight)  # type: ignore[union-attr]

ALL_PLUGINS = [*preflight.REQUIRED_PLUGINS, "kubernetes", "configuration-as-code"]


class FakeController:
    url = "https://jenkins.example"

    def __init__(self, *, plugins=ALL_PLUGINS, statuses: dict[str, int] | None = None) -> None:
        self.plugins = plugins
        self.statuses = statuses or {}
        self.paths: list[str] = []

    def get(self, path: str):
        self.paths.append(path)
        status = self.statuses.get(path, 200)
        if path.startswith("/manage/configure"):
            return status, {}, b'<input value="netci-shared-library">'
        return status, {}, b""

    def json(self, path: str):
        self.paths.append(path)
        status = self.statuses.get(path.split("?", 1)[0], 200)
        if path.startswith("/me/"):
            return status, {"id": "netci-sa"}
        if path.startswith("/crumbIssuer/"):
            return status, {"crumb": "c"}
        if path.startswith("/pluginManager/"):
            return status, {"plugins": [{"shortName": name, "active": True} for name in self.plugins]}
        if path.startswith("/credentials/"):
            return status, {"credentials": [{"id": "netci-cosign-key"}]}
        if path.startswith("/label/"):
            return status, {"clouds": [{"name": "kubernetes"}], "nodes": []}
        return status, {}


def _run(controller, capsys, **kwargs):
    report = preflight.Report()
    options = dict(library="netci-shared-library", cosign_id="netci-cosign-key", agent_label="netci-ephemeral")
    options.update(kwargs)
    preflight.check(controller, report=report, **options)
    return report, capsys.readouterr().out


def test_a_complete_controller_passes(capsys):
    report, out = _run(FakeController(), capsys)
    assert report.failed == 0, out
    assert "FAIL" not in out


@pytest.mark.parametrize("plugin", ["pipeline-utility-steps", "ws-cleanup"])
def test_the_plugins_the_library_steps_need_are_required(plugin, capsys):
    report, out = _run(FakeController(plugins=[p for p in ALL_PLUGINS if p != plugin]), capsys)
    assert report.failed == 1
    assert "FAIL  plugins" in out and plugin in out


def test_configuration_as_code_absent_is_info_not_a_failure(capsys):
    report, out = _run(FakeController(plugins=[p for p in ALL_PLUGINS if p != "configuration-as-code"]), capsys)
    assert report.failed == 0
    assert "INFO  jcasc plugin" in out and "absent" in out


def test_an_account_without_admin_learns_drift_and_reload_will_be_unreachable_as_info(capsys):
    report, out = _run(FakeController(statuses={"/configuration-as-code/": 403}), capsys)
    assert report.failed == 0
    assert "INFO  drift/reload" in out and "unreachable" in out


def test_a_library_version_is_reported_not_checked_rather_than_passed(capsys):
    report, out = _run(FakeController(), capsys, library="netci-shared-library@netci-0.2")
    assert report.failed == 0
    [line] = [line for line in out.splitlines() if "library version" in line]
    assert line.startswith("NOT CHECKED") and "netci-0.2" in line
    # The PASS line for the library no longer vouches for the version.
    [library_line] = [line for line in out.splitlines() if line.startswith("PASS  library ")]
    assert "netci-0.2" not in library_line


def test_job_cancel_is_named_and_marked_not_checked(capsys):
    _, out = _run(FakeController(statuses={"/view/all/newJob": 403}), capsys)
    assert "Cancel" in out
    assert any(line.startswith("NOT CHECKED  build/cancel") for line in out.splitlines())


def test_with_a_folder_the_permission_is_checked_in_the_folder(capsys):
    controller = FakeController(statuses={"/view/all/newJob": 403})
    report, out = _run(controller, capsys, folder="platform/netci")
    assert report.failed == 0, out
    assert "/job/platform/job/netci/newJob" in controller.paths
    assert "/view/all/newJob" not in controller.paths
    assert "PASS  folder" in out


def test_a_missing_folder_fails(capsys):
    controller = FakeController(statuses={"/job/platform/job/netci/api/json": 404})
    report, out = _run(controller, capsys, folder="platform/netci")
    assert "FAIL  folder" in out
    assert report.failed >= 1


def test_a_folder_without_create_permission_fails(capsys):
    controller = FakeController(statuses={"/job/platform/job/netci/newJob": 403})
    report, out = _run(controller, capsys, folder="platform/netci")
    assert report.failed == 1 and "FAIL  permissions" in out


def test_the_preflight_folder_rule_is_netcis():
    from app.adapters import jenkins_http

    assert preflight.FOLDER_SEGMENT.pattern == jenkins_http._FOLDER_SEGMENT.pattern


def test_a_folder_netci_would_refuse_is_refused_before_any_request(monkeypatch, capsys):
    monkeypatch.setenv("JENKINS_API_TOKEN", "t")
    monkeypatch.setattr("sys.argv", ["jenkins_preflight.py", "--url", "http://jenkins.invalid", "--user", "u",
                                     "--folder", "platform/../admin"])
    monkeypatch.setattr(preflight, "Controller", lambda *a, **k: pytest.fail("contacted Jenkins"))
    assert preflight.main() == 2
    assert "'..'" in capsys.readouterr().err
