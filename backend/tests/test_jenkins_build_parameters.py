"""What netCI tells Jenkins to build, and what it lets a caller decide.

Neither the application directory nor the image name reached Jenkins, so the CI scripts
used their defaults -- `sample-apps/hello-container`, image `hello-container` -- and every
container module built and pushed that one sample app under that one name. The lab
registry held `hello-container` and no `payment-gateway` or `shop-api` at all, after 7
and 4 "successful" builds of those. These pin the parameters the real adapter sends.
"""

from __future__ import annotations

import urllib.parse
from uuid import uuid4

import pytest

from app.adapters.ci_launcher import CiLaunchRequest
from app.adapters.jenkins_http import JOB_PARAMETERS, JenkinsHttpAdapter, JenkinsHttpConfig, image_name_for
from app.build_inputs import BuildInputError, application_directory, validate_build_inputs


def _request(**overrides) -> CiLaunchRequest:
    fields = dict(
        application_id=uuid4(), application_name="payment-gateway",
        repository_url="https://git.example/payments/payment-gateway.git",
        pipeline_template="container-ci-cd-v1", runtime="docker", stages=("checkout", "build"),
        pipeline_run_id=uuid4(), commit_sha="a" * 40, branch="main", environment="dev",
        correlation_id="c-1", parameters={},
    )
    fields.update(overrides)
    return CiLaunchRequest(**fields)


def _sent_query(request: CiLaunchRequest) -> dict[str, str]:
    adapter = JenkinsHttpAdapter(JenkinsHttpConfig(base_url="http://jenkins", username="u", api_token="t"))
    sent: dict[str, str] = {}

    def fake(method, path, *args, **kwargs):
        if "buildWithParameters" in path:
            sent.update(dict(urllib.parse.parse_qsl(path.split("?", 1)[1], keep_blank_values=True)))
            return 201, {"Location": "http://jenkins/queue/item/7/"}, b""
        raise RuntimeError("stop after the trigger")

    adapter._request = fake  # type: ignore[method-assign]
    with pytest.raises(Exception):
        adapter.trigger_ci_run("netci-x", request, callback_token="tok")
    return sent


def test_the_build_is_told_which_directory_and_which_image_name():
    sent = _sent_query(_request())
    assert sent["NETCI_APP_DIR"] == "."
    assert sent["NETCI_IMAGE_NAME"] == "payment-gateway"


def test_a_monorepo_module_builds_its_own_subdirectory():
    sent = _sent_query(_request(parameters={"NETCI_APP_DIR": "services/payments"}))
    assert sent["NETCI_APP_DIR"] == "services/payments"


def test_both_are_declared_on_the_job_or_jenkins_would_drop_them():
    # buildWithParameters ignores a parameter the job does not declare.
    assert "NETCI_APP_DIR" in JOB_PARAMETERS
    assert "NETCI_IMAGE_NAME" in JOB_PARAMETERS


def test_a_caller_cannot_choose_the_image_name():
    # Where the artifact is published belongs to the server: one module must not be
    # able to publish as another by naming its image.
    with pytest.raises(BuildInputError) as refused:
        validate_build_inputs({"NETCI_IMAGE_NAME": "payment-gateway"})
    assert refused.value.code == "BUILD_INPUT_NOT_ALLOWED"


@pytest.mark.parametrize("path", ["../other-repo", "/etc", "a/../../b", "services//x", "./x", "a b"])
def test_an_application_directory_outside_the_checkout_is_refused(path):
    with pytest.raises(BuildInputError):
        application_directory(path)


@pytest.mark.parametrize("path, expected", [(".", "."), ("", "."), ("services/payments", "services/payments"),
                                            ("services/payments/", "services/payments")])
def test_plain_relative_directories_are_accepted(path, expected):
    assert application_directory(path) == expected


def test_image_names_are_registry_safe():
    assert image_name_for("Payment Gateway") == "payment-gateway"
    with pytest.raises(ValueError):
        image_name_for("---")


# ---------------------------------------------------------------- verify-only builds (ADR-043)


def test_a_verify_only_build_is_told_so_and_names_no_stage_that_signs_or_publishes():
    sent = _sent_query(_request(publish_artifact=False, stages=("checkout", "unit-test", "build", "sign", "publish")))
    assert sent["NETCI_PUBLISH"] == "false"
    # An older library ignores NETCI_PUBLISH but has always honoured NETCI_STAGES.
    assert sent["NETCI_STAGES"].split(",") == ["checkout", "unit-test", "build"]


def test_a_verify_only_build_with_no_stage_list_still_names_one_without_sign():
    sent = _sent_query(_request(publish_artifact=False, stages=()))
    stages = sent["NETCI_STAGES"].split(",")
    assert "sign" not in stages and "publish" not in stages and "build" in stages


def test_a_published_build_keeps_its_stages_and_is_told_to_publish():
    sent = _sent_query(_request(stages=("checkout", "build", "sign", "publish")))
    assert sent["NETCI_PUBLISH"] == "true"
    assert sent["NETCI_STAGES"] == "checkout,build,sign,publish"


def test_the_pull_request_ref_reaches_jenkins_only_in_its_validated_form():
    assert _sent_query(_request(source_ref="refs/pull/12/head"))["NETCI_GIT_REF"] == "refs/pull/12/head"
    assert _sent_query(_request())["NETCI_GIT_REF"] == ""


def test_the_job_declares_the_new_parameters():
    assert {"NETCI_PUBLISH", "NETCI_GIT_REF"} <= set(JOB_PARAMETERS)
