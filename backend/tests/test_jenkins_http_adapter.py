"""The Jenkins REST adapter against the behaviour of a company controller.

A pre-integration review against a shared, company-run Jenkins found what the lab's own
controllers never showed: a quiet period and busy agents leave most builds in the queue
past the trigger's ten-second wait, and such a run could then never be read or stopped;
the callback token rode in the URL into access logs; a folder is where the service
account may create jobs; other teams' queued builds count in the same queue. Each test
names one of those.
"""

from __future__ import annotations

import io
import json
import urllib.error
import urllib.parse
import urllib.request
from uuid import uuid4

import pytest

from app.adapters import jenkins_http
from app.adapters.ci_launcher import CiLaunchRequest, JenkinsCiLauncher
from app.adapters.jenkins_http import (
    JenkinsHttpAdapter,
    JenkinsHttpConfig,
    JenkinsHttpError,
    jenkins_folder,
    queued_run_id,
)
from app.adapters.jenkins_router import JenkinsController, JenkinsRouter

RUN_ID = "5b0f0f4e-1111-4a4a-9b9b-0123456789ab"
JOB = "netci-7a1e3c52-0000-4000-8000-000000000001"


def _adapter(**config) -> JenkinsHttpAdapter:
    return JenkinsHttpAdapter(JenkinsHttpConfig(base_url="http://jenkins", username="u", api_token="t", **config))


class FakeJenkins:
    """Answers `_request` from a table of (method, path) -> (status, headers, body) or an
    exception, and records every call. Paths are matched exactly, query string included."""

    def __init__(self, routes: dict[tuple[str, str], object]) -> None:
        self.routes = dict(routes)
        self.calls: list[tuple[str, str, bytes | None]] = []

    def __call__(self, method, path, *, body=None, content_type="application/json", use_crumb=True):
        self.calls.append((method, path, body))
        answer = self.routes.get((method, path))
        if answer is None:
            raise JenkinsHttpError(404, f"Jenkins {method} {path} failed: Not Found")
        if isinstance(answer, Exception):
            raise answer
        if callable(answer):
            answer = answer()
        status, headers, payload = answer
        if isinstance(payload, (dict, list)):
            payload = json.dumps(payload).encode()
        return status, headers, payload

    def posted(self) -> list[str]:
        return [path for method, path, _ in self.calls if method == "POST"]


def _wire(adapter: JenkinsHttpAdapter, routes) -> FakeJenkins:
    fake = FakeJenkins(routes)
    adapter._request = fake  # type: ignore[method-assign]
    return fake


def _ok(payload) -> tuple[int, dict, object]:
    return 200, {}, payload


def _launch_request(**overrides) -> CiLaunchRequest:
    fields = dict(
        application_id=uuid4(), application_name="payments-api",
        repository_url="https://git.example/payments/payments-api.git",
        pipeline_template="container-ci-cd-v1", runtime="docker", stages=("checkout", "build"),
        pipeline_run_id=uuid4(), commit_sha="a" * 40, branch="main", environment="dev",
        correlation_id="c-1", parameters={},
    )
    fields.update(overrides)
    return CiLaunchRequest(**fields)


def _builds(*entries) -> dict:
    return {"builds": [
        {"number": number, "queueId": queue_id,
         "actions": [{}, {"parameters": [{"name": "NETCI_PIPELINE_RUN_ID", "value": run_id},
                                          {"name": "NETCI_CALLBACK_TOKEN"}]}]}
        for number, queue_id, run_id in entries
    ]}


BUILDS_LOOKUP = (
    f"/job/{JOB}/api/json?tree="
    + urllib.parse.quote("builds[number,queueId,actions[parameters[name,value]]]{0,50}", safe=",")
)


# ------------------------------------------------------------ queued run ids


def test_a_build_still_queued_after_the_wait_keeps_its_queue_id_and_pipeline_run(monkeypatch):
    monkeypatch.setattr(jenkins_http.time, "sleep", lambda _seconds: None)
    adapter = _adapter()
    request = _launch_request()
    _wire(adapter, {
        ("POST", f"/job/{JOB}/buildWithParameters"): (201, {"Location": "http://jenkins/queue/item/41/"}, b""),
        ("GET", "/queue/item/41/api/json"): _ok({"why": "In the quiet period", "executable": None}),
    })
    run = adapter.trigger_ci_run(JOB, request, callback_token="tok")
    assert run.status == "queued"
    assert run.run_id == f"{JOB}@q41/{request.pipeline_run_id}"


def test_a_build_that_started_in_time_keeps_the_resolved_form(monkeypatch):
    adapter = _adapter()
    _wire(adapter, {
        ("POST", f"/job/{JOB}/buildWithParameters"): (201, {"Location": "http://jenkins/queue/item/41/"}, b""),
        ("GET", "/queue/item/41/api/json"): _ok({"executable": {"number": 12, "url": f"http://jenkins/job/{JOB}/12/"}}),
    })
    run = adapter.trigger_ci_run(JOB, _launch_request(), callback_token="tok")
    assert (run.run_id, run.status) == (f"{JOB}#12", "running")


def test_a_queued_run_still_waiting_reads_as_queued_which_is_not_terminal():
    adapter = _adapter()
    _wire(adapter, {("GET", "/queue/item/41/api/json"): _ok({"why": "Waiting for next available executor"})})
    assert adapter.get_status(queued_run_id(JOB, "41", RUN_ID)).status == "queued"


def test_a_queued_run_that_started_reports_its_build_and_the_resolved_id():
    adapter = _adapter()
    _wire(adapter, {
        ("GET", "/queue/item/41/api/json"): _ok({"executable": {"number": 12}}),
        ("GET", f"/job/{JOB}/12/api/json"): _ok({"building": False, "result": "SUCCESS", "url": "http://jenkins/x/12/"}),
    })
    run = adapter.get_status(queued_run_id(JOB, "41", RUN_ID))
    assert (run.run_id, run.status) == (f"{JOB}#12", "succeeded")


def test_a_cancelled_queue_item_reads_as_cancelled():
    adapter = _adapter()
    _wire(adapter, {("GET", "/queue/item/41/api/json"): _ok({"cancelled": True})})
    assert adapter.get_status(queued_run_id(JOB, "41", RUN_ID)).status == "cancelled"


def test_after_jenkins_forgets_the_queue_item_the_build_is_found_by_its_pipeline_run_parameter():
    adapter = _adapter()
    other_run = str(uuid4())
    fake = _wire(adapter, {
        # /queue/item/41 is gone: absent from the table, so 404.
        ("GET", BUILDS_LOOKUP): _ok(_builds((14, 43, other_run), (13, 41, RUN_ID), (12, 40, str(uuid4())))),
        ("GET", f"/job/{JOB}/13/api/json"): _ok({"building": True}),
    })
    run = adapter.get_status(queued_run_id(JOB, "41", RUN_ID))
    assert (run.run_id, run.status) == (f"{JOB}#13", "running")
    assert ("GET", "/queue/item/41/api/json", None) in fake.calls


def test_a_forgotten_queue_item_with_no_matching_build_is_unknown_not_a_status():
    adapter = _adapter()
    _wire(adapter, {("GET", BUILDS_LOOKUP): _ok(_builds((12, 40, str(uuid4()))))})
    with pytest.raises(JenkinsHttpError) as unknown:
        adapter.get_status(queued_run_id(JOB, "41", RUN_ID))
    assert unknown.value.status == 404


def test_a_queue_read_that_fails_for_another_reason_is_not_taken_for_a_forgotten_item():
    adapter = _adapter()
    fake = _wire(adapter, {("GET", "/queue/item/41/api/json"): JenkinsHttpError(503, "Jenkins unavailable")})
    with pytest.raises(JenkinsHttpError):
        adapter.get_status(queued_run_id(JOB, "41", RUN_ID))
    assert not any(path == BUILDS_LOOKUP for _, path, _ in fake.calls)


def test_a_trigger_without_a_queue_location_is_still_found_by_its_parameter():
    adapter = _adapter()
    _wire(adapter, {
        ("GET", BUILDS_LOOKUP): _ok(_builds((9, 5, RUN_ID))),
        ("GET", f"/job/{JOB}/9/api/json"): _ok({"result": "FAILURE"}),
    })
    assert adapter.get_status(f"{JOB}@q/{RUN_ID}").status == "failed"


def test_run_ids_stored_before_this_change_still_parse():
    adapter = _adapter()
    _wire(adapter, {
        ("GET", "/queue/item/41/api/json"): _ok({"executable": {"number": 3}}),
        ("GET", f"/job/{JOB}/3/api/json"): _ok({"result": "ABORTED"}),
        ("GET", f"/job/{JOB}/7/api/json"): _ok({"result": "SUCCESS"}),
    })
    assert adapter.get_status(f"{JOB}@41").status == "cancelled"
    assert adapter.get_status(f"{JOB}#7").status == "succeeded"
    # `@queue` carried nothing to find the build by; it stays unknown rather than guessed.
    with pytest.raises(JenkinsHttpError):
        adapter.get_status(f"{JOB}@queue")


def test_the_launcher_reports_a_queued_run_as_queued_through_the_controller_prefix():
    adapter = _adapter()
    _wire(adapter, {("GET", "/queue/item/41/api/json"): _ok({})})
    launcher = JenkinsCiLauncher(JenkinsRouter([JenkinsController("jenkins-a", executors_total=2)]),
                                 {"jenkins-a": adapter})
    assert launcher.get_status(f"jenkins-a:{queued_run_id(JOB, '41', RUN_ID)}") == "queued"
    # Unknown stays None: the reconciler then only applies its timeout.
    assert launcher.get_status(f"jenkins-a:{JOB}@queue") is None


# ------------------------------------------------------------------- abort


def test_aborting_a_queued_run_cancels_the_queue_item_not_a_build():
    adapter = _adapter()
    state = {"cancelled": False}
    fake = _wire(adapter, {
        ("GET", "/queue/item/41/api/json"): lambda: _ok({"cancelled": state["cancelled"]}),
        ("POST", "/queue/cancelItem?id=41"): lambda: (state.update(cancelled=True), (204, {}, b""))[1],
    })
    adapter.abort(queued_run_id(JOB, "41", RUN_ID))
    assert fake.posted() == ["/queue/cancelItem?id=41"]


def test_aborting_a_queued_run_that_started_meanwhile_stops_the_build():
    adapter = _adapter()
    reads = iter([_ok({}), _ok({"executable": {"number": 12}})])
    fake = _wire(adapter, {
        ("GET", "/queue/item/41/api/json"): lambda: next(reads),
        # A core that answers a cancel of an item no longer in the queue with 404.
        ("POST", "/queue/cancelItem?id=41"): JenkinsHttpError(404, "not found"),
        ("POST", f"/job/{JOB}/12/stop"): (200, {}, b""),
    })
    adapter.abort(queued_run_id(JOB, "41", RUN_ID))
    assert fake.posted() == ["/queue/cancelItem?id=41", f"/job/{JOB}/12/stop"]


def test_a_cancel_jenkins_refused_is_reported_not_swallowed():
    adapter = _adapter()
    _wire(adapter, {
        ("GET", "/queue/item/41/api/json"): _ok({}),
        ("POST", "/queue/cancelItem?id=41"): JenkinsHttpError(403, "lacks Job/Cancel"),
    })
    with pytest.raises(JenkinsHttpError) as refused:
        adapter.abort(queued_run_id(JOB, "41", RUN_ID))
    assert refused.value.status == 403


def test_aborting_a_resolved_run_stops_the_build_as_before():
    adapter = _adapter()
    fake = _wire(adapter, {("POST", f"/job/{JOB}/12/stop"): (200, {}, b"")})
    adapter.abort(f"{JOB}#12")
    assert fake.posted() == [f"/job/{JOB}/12/stop"]


def test_aborting_after_the_queue_item_is_forgotten_stops_the_build_found_by_parameter():
    adapter = _adapter()
    fake = _wire(adapter, {
        ("GET", BUILDS_LOOKUP): _ok(_builds((13, 41, RUN_ID))),
        ("POST", f"/job/{JOB}/13/stop"): (200, {}, b""),
    })
    adapter.abort(queued_run_id(JOB, "41", RUN_ID))
    assert fake.posted() == [f"/job/{JOB}/13/stop"]


def test_aborting_an_already_cancelled_queue_item_posts_nothing():
    adapter = _adapter()
    fake = _wire(adapter, {("GET", "/queue/item/41/api/json"): _ok({"cancelled": True})})
    adapter.abort(queued_run_id(JOB, "41", RUN_ID))
    assert fake.posted() == []


# ------------------------------------------------- the callback token and the URL


class _Response:
    def __init__(self, status: int, headers: dict[str, str], body: bytes) -> None:
        self.status, self.headers, self._body = status, headers, body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        return None


class _Opener:
    def __init__(self, fail_trigger: bool = False) -> None:
        self.requests: list[urllib.request.Request] = []
        self.fail_trigger = fail_trigger

    def open(self, request, timeout=None):
        self.requests.append(request)
        if request.full_url.endswith("/crumbIssuer/api/json"):
            return _Response(200, {}, b'{"crumbRequestField": "Jenkins-Crumb", "crumb": "c"}')
        if "buildWithParameters" in request.full_url:
            if self.fail_trigger:
                raise urllib.error.HTTPError(request.full_url, 500, "boom", {}, io.BytesIO(b"server error"))
            return _Response(201, {"Location": "http://jenkins/queue/item/41/"}, b"")
        return _Response(200, {}, b'{"executable": {"number": 3, "url": "http://jenkins/job/x/3/"}}')



def test_the_callback_token_is_sent_in_the_form_body_never_in_the_url():
    adapter = _adapter()
    opener = _Opener()
    adapter._opener = opener  # type: ignore[assignment]
    adapter.trigger_ci_run(JOB, _launch_request(), callback_token="s3cr3t-callback-token")
    [trigger] = [r for r in opener.requests if "buildWithParameters" in r.full_url]
    assert "s3cr3t-callback-token" not in trigger.full_url
    assert "?" not in trigger.full_url  # no parameter value of any kind rides in the URL
    assert trigger.get_method() == "POST"
    assert trigger.get_header("Content-type") == "application/x-www-form-urlencoded"
    form = dict(urllib.parse.parse_qsl(trigger.data.decode(), keep_blank_values=True))
    assert form["NETCI_CALLBACK_TOKEN"] == "s3cr3t-callback-token"
    assert trigger.get_header("Jenkins-crumb") == "c"


def test_a_refused_trigger_does_not_carry_the_token_into_the_error_it_raises():
    # The error message is logged by the launcher and stored in the run's launch-failure
    # log and audit record; with the parameters in the URL, the token went there too.
    adapter = _adapter()
    adapter._opener = _Opener(fail_trigger=True)  # type: ignore[assignment]
    with pytest.raises(JenkinsHttpError) as refused:
        adapter.trigger_ci_run(JOB, _launch_request(), callback_token="s3cr3t-callback-token")
    assert "s3cr3t-callback-token" not in str(refused.value)


# ----------------------------------------------------------- JCasC calls


def test_a_non_2xx_jcasc_export_raises_a_jenkins_error_not_a_type_error():
    adapter = _adapter()
    _wire(adapter, {("POST", "/configuration-as-code/export"): (302, {}, b"")})
    with pytest.raises(JenkinsHttpError) as refused:
        adapter.configuration_fingerprint()
    assert refused.value.status == 302


def test_a_non_2xx_jcasc_reload_raises_a_jenkins_error_not_a_type_error():
    adapter = _adapter()
    _wire(adapter, {("POST", "/configuration-as-code/reload"): (302, {}, b"<p>moved</p>")})
    with pytest.raises(JenkinsHttpError) as refused:
        adapter.reload_configuration()
    assert refused.value.status == 302 and "moved" in refused.value.message


def test_export_and_reload_use_the_same_plugin_root():
    adapter = _adapter()
    fake = _wire(adapter, {
        ("POST", "/configuration-as-code/export"): _ok(b"jenkins: {}\n"),
        ("GET", "/pluginManager/api/json?depth=1&tree=plugins%5BshortName,version%5D"): _ok({"plugins": []}),
        ("GET", "/api/json?tree=jobs%5Bname%5D"): _ok({"jobs": []}),
        ("POST", "/configuration-as-code/reload"): _ok(b""),
    })
    adapter.configuration_fingerprint()
    adapter.reload_configuration()
    assert fake.posted() == ["/configuration-as-code/export", "/configuration-as-code/reload"]


# ---------------------------------------------------------- job creation

CREATE = "/createItem?name={name}&mode=org.jenkinsci.plugins.workflow.job.WorkflowJob"


def _job_routes(adapter, application_id, create_answer, exists: bool, prefix: str = ""):
    name = adapter.job_name(application_id)
    routes = {("POST", prefix + CREATE.format(name=name)): create_answer,
              ("POST", f"{prefix}/job/{name}/config.xml"): (200, {}, b"")}
    if exists:
        routes[("GET", f"{prefix}/job/{name}/api/json?tree=name")] = _ok({"name": name})
    return name, routes


def test_an_existing_job_is_reconfigured():
    adapter = _adapter()
    application_id = uuid4()
    name, routes = _job_routes(adapter, application_id, JenkinsHttpError(400, "A job already exists with the name"), True)
    fake = _wire(adapter, routes)
    assert adapter.create_or_update_job(application_id, "container-ci-cd-v1") == name
    assert fake.posted()[-1] == f"/job/{name}/config.xml"


def test_a_409_is_taken_as_exists():
    adapter = _adapter()
    application_id = uuid4()
    name, routes = _job_routes(adapter, application_id, JenkinsHttpError(409, "conflict"), False)
    fake = _wire(adapter, routes)
    adapter.create_or_update_job(application_id, "container-ci-cd-v1")
    assert fake.posted()[-1] == f"/job/{name}/config.xml"


def test_any_other_400_raises_with_jenkins_message_instead_of_overwriting_config():
    adapter = _adapter()
    application_id = uuid4()
    refusal = JenkinsHttpError(400, "Jenkins POST /createItem failed: <html><body><p>No such job type: "
                                    "org.jenkinsci.plugins.workflow.job.WorkflowJob</p></body></html>")
    name, routes = _job_routes(adapter, application_id, refusal, False)
    fake = _wire(adapter, routes)
    with pytest.raises(JenkinsHttpError) as refused:
        adapter.create_or_update_job(application_id, "container-ci-cd-v1")
    assert refused.value.status == 400
    assert "No such job type" in refused.value.message and "<p>" not in refused.value.message
    assert not any(path.endswith("/config.xml") for path in fake.posted())


# ------------------------------------------------------------------ folders


@pytest.mark.parametrize("value, expected", [("", ""), ("platform/netci", "platform/netci"),
                                             ("/platform/netci/", "platform/netci"), ("netci", "netci")])
def test_folder_paths_are_normalised(value, expected):
    assert jenkins_folder(value) == expected


@pytest.mark.parametrize("value", ["..", "platform/../admin", "a//b", "team x", "a?b", "a#b", "-x", "a/%2e%2e"])
def test_unsafe_folder_segments_are_refused(value):
    with pytest.raises(ValueError):
        jenkins_folder(value)


def test_an_unsafe_folder_stops_configuration(monkeypatch):
    monkeypatch.setenv("JENKINS_URL", "http://jenkins")
    monkeypatch.setenv("JENKINS_USERNAME", "u")
    monkeypatch.setenv("JENKINS_API_TOKEN", "t")
    monkeypatch.setenv("NETCI_JENKINS_FOLDER", "platform/../admin")
    with pytest.raises(ValueError):
        JenkinsHttpConfig.from_env()
    monkeypatch.setenv("NETCI_JENKINS_FOLDER", "platform/netci")
    assert JenkinsHttpConfig.from_env().folder == "platform/netci"
    monkeypatch.delenv("NETCI_JENKINS_FOLDER")
    assert JenkinsHttpConfig.from_env().folder == ""


def test_an_adapter_built_in_code_refuses_an_unsafe_folder_too():
    with pytest.raises(ValueError):
        _adapter(folder="../x")


def test_every_job_call_goes_through_the_folder(monkeypatch):
    monkeypatch.setattr(jenkins_http.time, "sleep", lambda _seconds: None)
    adapter = _adapter(folder="platform/netci")
    prefix = "/job/platform/job/netci"
    application_id = uuid4()
    name, routes = _job_routes(adapter, application_id, JenkinsHttpError(409, "exists"), False, prefix=prefix)
    lookup = BUILDS_LOOKUP.replace(f"/job/{JOB}/", f"{prefix}/job/{name}/")
    routes.update({
        ("POST", f"{prefix}/job/{name}/buildWithParameters"): (201, {"Location": "http://jenkins/queue/item/41/"}, b""),
        ("GET", "/queue/item/41/api/json"): _ok({}),
        ("GET", lookup): _ok(_builds((2, 41, RUN_ID))),
        ("GET", f"{prefix}/job/{name}/2/api/json"): _ok({"building": True}),
        ("POST", f"{prefix}/job/{name}/2/stop"): (200, {}, b""),
    })
    fake = _wire(adapter, routes)
    assert adapter.create_or_update_job(application_id, "container-ci-cd-v1") == name
    run = adapter.trigger_ci_run(name, _launch_request(), callback_token="tok")
    assert run.run_id.startswith(f"{name}@q41/")
    # Jenkins forgets the queue item; the build is found inside the folder.
    del fake.routes[("GET", "/queue/item/41/api/json")]
    assert adapter.get_status(queued_run_id(name, "41", RUN_ID)).status == "running"
    adapter.abort(f"{name}#2")
    assert fake.posted() == [
        prefix + CREATE.format(name=name), f"{prefix}/job/{name}/config.xml",
        f"{prefix}/job/{name}/buildWithParameters", f"{prefix}/job/{name}/2/stop",
    ]


def test_a_missing_folder_is_named_in_the_error_and_never_created():
    adapter = _adapter(folder="platform/netci")
    fake = _wire(adapter, {})  # everything 404s, as Jenkins does for a folder that is not there
    with pytest.raises(JenkinsHttpError) as missing:
        adapter.create_or_update_job(uuid4(), "container-ci-cd-v1")
    assert missing.value.status == 404 and "'platform/netci'" in missing.value.message
    assert all("createItem" in path for path in fake.posted())  # no attempt to create the folder


# ------------------------------------------------------------- queue budget


QUEUE = "/queue/api/json?tree=items[id,task[name,url]]"


def test_only_netcis_own_queued_items_count_against_the_budget():
    adapter = _adapter()
    _wire(adapter, {("GET", QUEUE): _ok({"items": [
        {"id": 1, "task": {"name": f"{JOB}", "url": f"http://jenkins/job/{JOB}/"}},
        {"id": 2, "task": {"name": "billing-nightly", "url": "http://jenkins/job/billing-nightly/"}},
        {"id": 3, "task": {"name": "release", "url": "http://jenkins/job/team-a/job/release/"}},
        {"id": 4},
    ]})})
    assert adapter.queued_builds() == 1


def test_with_a_folder_only_items_under_it_count():
    adapter = _adapter(folder="platform/netci")
    _wire(adapter, {("GET", QUEUE): _ok({"items": [
        {"id": 1, "task": {"name": JOB, "url": f"https://ci.example/jenkins/job/platform/job/netci/job/{JOB}/"}},
        {"id": 2, "task": {"name": JOB, "url": f"https://ci.example/jenkins/job/other/job/{JOB}/"}},
        {"id": 3, "task": {"name": JOB, "url": f"https://ci.example/jenkins/job/{JOB}/"}},
    ]})})
    assert adapter.queued_builds() == 1


def test_an_unreadable_queue_is_still_unknown():
    adapter = _adapter()
    _wire(adapter, {("GET", QUEUE): _ok({"unexpected": True})})
    assert adapter.queued_builds() is None
