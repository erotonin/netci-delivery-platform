from __future__ import annotations

import base64
import http.cookiejar
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from uuid import UUID
from xml.sax.saxutils import escape

from .interfaces import JenkinsRun

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids an import cycle at runtime
    from .ci_launcher import CiLaunchRequest


class JenkinsHttpError(RuntimeError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def _read_secret(name: str) -> str:
    """Prefer a mounted secret file over an inline environment value."""

    path = os.getenv(f"{name}_FILE", "").strip()
    if path:
        with open(path, encoding="utf-8") as handle:
            return handle.read().strip()
    return os.getenv(name, "").strip()


@dataclass(frozen=True)
class JenkinsHttpConfig:
    base_url: str
    username: str
    api_token: str
    timeout_seconds: float = 10.0
    callback_url: str = "http://host.docker.internal:8000"
    callback_credentials_id: str = "netci-pipeline-api-key"
    agent_label: str = "netci-ephemeral"
    shared_library: str = "netci-shared-library"
    # Where the build pushes and pulls. A build agent generally cannot reach the public
    # internet, so the base image comes from the same registry as everything else.
    registry_push_host: str = ""
    registry_pull_host: str = ""
    base_image: str = ""
    # Where Trivy fetches its vulnerability database. A build farm without internet
    # access needs a mirror; a stale or missing database would silently weaken the scan.
    trivy_db_repository: str = ""
    cosign_credentials_id: str = "netci-cosign-key"

    @classmethod
    def from_env(cls, prefix: str = "JENKINS") -> "JenkinsHttpConfig":
        base_url = _read_secret(f"{prefix}_URL").rstrip("/")
        username = _read_secret(f"{prefix}_USERNAME")
        api_token = _read_secret(f"{prefix}_API_TOKEN")
        if not base_url or not username or not api_token:
            raise ValueError(f"{prefix}_URL, {prefix}_USERNAME and {prefix}_API_TOKEN are required")
        return cls(
            base_url=base_url,
            username=username,
            api_token=api_token,
            timeout_seconds=float(os.getenv(f"{prefix}_TIMEOUT_SECONDS", "10")),
            callback_url=os.getenv("NETCI_CALLBACK_URL", "http://host.docker.internal:8000").rstrip("/"),
            callback_credentials_id=os.getenv("NETCI_CALLBACK_CREDENTIALS_ID", "netci-pipeline-api-key"),
            agent_label=os.getenv("NETCI_AGENT_LABEL", "netci-ephemeral"),
            shared_library=os.getenv("NETCI_SHARED_LIBRARY", "netci-shared-library"),
            registry_push_host=os.getenv("NETCI_REGISTRY_PUSH_HOST", ""),
            registry_pull_host=os.getenv("NETCI_REGISTRY_PULL_HOST", ""),
            base_image=os.getenv("NETCI_BUILD_BASE_IMAGE", ""),
            trivy_db_repository=os.getenv("NETCI_TRIVY_DB_REPOSITORY", ""),
            cosign_credentials_id=os.getenv("NETCI_COSIGN_CREDENTIALS_ID", "netci-cosign-key"),
        )


def _parameter_xml(name: str, default: str = "") -> str:
    if name in SECRET_PARAMETERS:
        # A password parameter is masked in the build page, the console log and the
        # environment listing. The callback token is a credential for one run; it must
        # not be readable by everyone who can open the build.
        return (
            "<hudson.model.PasswordParameterDefinition>"
            f"<name>{escape(name)}</name>"
            "<defaultValue></defaultValue>"
            "</hudson.model.PasswordParameterDefinition>"
        )
    return (
        "<hudson.model.StringParameterDefinition>"
        f"<name>{escape(name)}</name>"
        f"<defaultValue>{escape(default)}</defaultValue>"
        "<trim>true</trim>"
        "</hudson.model.StringParameterDefinition>"
    )


#: Parameters that are credentials. Declared masked in the job and never logged here.
SECRET_PARAMETERS: frozenset[str] = frozenset({"NETCI_CALLBACK_TOKEN"})


#: Every value netCI hands a build.  Declared once so the job XML and the trigger
#: call cannot drift apart.
JOB_PARAMETERS: tuple[str, ...] = (
    "NETCI_PIPELINE_RUN_ID",
    "NETCI_CALLBACK_TOKEN",
    "NETCI_APPLICATION_ID",
    "NETCI_CORRELATION_ID",
    "NETCI_API_URL",
    "NETCI_ENVIRONMENT",
    "NETCI_TEMPLATE",
    "NETCI_STAGES",
    "GIT_URL",
    "GIT_BRANCH",
    "COMMIT_SHA",
    # Lets the same job be sent to a shared or an ephemeral agent, which is what the
    # benchmark compares. Empty means "use the template default".
    "NETCI_AGENT_LABEL",
    "NETCI_BASE_IMAGE",
    "REGISTRY_PUSH_HOST",
    "REGISTRY_PULL_HOST",
    "NETCI_TRIVY_DB_REPOSITORY",
)


class JenkinsHttpAdapter:
    """Jenkins REST adapter.

    Jobs are managed as **pipeline** jobs (`flow-definition` + `CpsFlowDefinition`)
    that load the `netciPipeline` shared library, so a build's stage graph comes from
    Git rather than from clicked-in freestyle steps.  No Jenkins detail leaks into the
    domain: callers see `JenkinsRun`.
    """

    def __init__(self, config: JenkinsHttpConfig) -> None:
        self.config = config
        self._crumb: tuple[str, str] | None = None
        # Jenkins issues a CSRF crumb bound to the HTTP session it was requested in.
        # Without a cookie jar the crumb is fetched in one session and presented in
        # another, and every POST is rejected with "No valid crumb was included".
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
        )

    # ---------------------------------------------------------------- transport

    def _url(self, path: str) -> str:
        return f"{self.config.base_url}/{path.lstrip('/')}"

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        content_type: str = "application/json",
        use_crumb: bool = True,
    ) -> tuple[int, dict[str, str], bytes]:
        def attempt() -> tuple[int, dict[str, str], bytes]:
            request = urllib.request.Request(self._url(path), data=body, method=method)
            credentials = f"{self.config.username}:{self.config.api_token}".encode()
            request.add_header("Authorization", f"Basic {base64.b64encode(credentials).decode()}")
            request.add_header("Accept", "application/json")
            if body is not None:
                request.add_header("Content-Type", content_type)
            if method == "POST" and use_crumb:
                crumb = self._crumb_header()
                if crumb is not None:
                    request.add_header(*crumb)
            with self._opener.open(request, timeout=self.config.timeout_seconds) as response:
                return response.status, dict(response.headers.items()), response.read()

        try:
            return attempt()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[-1000:]
            # A cached crumb outlives the Jenkins session it belongs to: after the
            # controller restarts, every POST is rejected until the crumb is refetched.
            # Discard it and retry once rather than staying broken until netCI restarts.
            if exc.code == 403 and method == "POST" and use_crumb and "crumb" in detail.lower():
                self._crumb = None
                try:
                    return attempt()
                except urllib.error.HTTPError as retry_exc:
                    detail = retry_exc.read().decode(errors="replace")[-1000:]
                    raise JenkinsHttpError(
                        retry_exc.code, f"Jenkins {method} {path} failed: {detail}"
                    ) from retry_exc
                except urllib.error.URLError as retry_exc:
                    raise JenkinsHttpError(503, f"Jenkins unavailable: {retry_exc.reason}") from retry_exc
            raise JenkinsHttpError(exc.code, f"Jenkins {method} {path} failed: {detail}") from exc
        except (urllib.error.URLError, OSError) as exc:
            reason = getattr(exc, "reason", str(exc))
            raise JenkinsHttpError(503, f"Jenkins unavailable: {reason}") from exc

    def _crumb_header(self) -> tuple[str, str] | None:
        if self._crumb is not None:
            return self._crumb
        try:
            _, _, body = self._request("GET", "/crumbIssuer/api/json", use_crumb=False)
        except (JenkinsHttpError, OSError):
            return None  # CSRF protection disabled, or API-token auth is exempt
        try:
            payload = json.loads(body or b"{}")
            self._crumb = (str(payload["crumbRequestField"]), str(payload["crumb"]))
        except (KeyError, ValueError):
            return None
        return self._crumb

    def health_check(self) -> bool:
        try:
            status, _, _ = self._request("GET", "/api/json?tree=mode", use_crumb=False)
            return 200 <= status < 300
        except (JenkinsHttpError, OSError):
            return False

    def queue_depth(self) -> int:
        try:
            _, _, body = self._request("GET", "/queue/api/json?tree=items[id]", use_crumb=False)
            return len(json.loads(body or b"{}").get("items", []))
        except (JenkinsHttpError, OSError, ValueError):
            return 0

    # ------------------------------------------------------------------- jobs

    @staticmethod
    def job_name(application_id: UUID) -> str:
        return f"netci-{application_id}"

    def _job_config_xml(self, template_id: str) -> bytes:
        script = (
            f"@Library('{self.config.shared_library}') _\n"
            "netciPipeline(\n"
            f"    template: params.NETCI_TEMPLATE ?: '{template_id}',\n"
            f"    agentLabel: '{self.config.agent_label}',\n"
            f"    callbackCredentialsId: '{self.config.callback_credentials_id}',\n"
            f"    cosignCredentialsId: '{self.config.cosign_credentials_id}'\n"
            ")\n"
        )
        parameters = "".join(_parameter_xml(name) for name in JOB_PARAMETERS)
        return (
            "<?xml version='1.1' encoding='UTF-8'?>\n"
            "<flow-definition plugin=\"workflow-job\">\n"
            f"  <description>Managed by netCI template {escape(template_id)}. Do not edit in the Jenkins UI.</description>\n"
            "  <keepDependencies>false</keepDependencies>\n"
            "  <properties>\n"
            "    <hudson.model.ParametersDefinitionProperty>\n"
            f"      <parameterDefinitions>{parameters}</parameterDefinitions>\n"
            "    </hudson.model.ParametersDefinitionProperty>\n"
            "  </properties>\n"
            "  <definition class=\"org.jenkinsci.plugins.workflow.cps.CpsFlowDefinition\" plugin=\"workflow-cps\">\n"
            f"    <script>{escape(script)}</script>\n"
            "    <sandbox>true</sandbox>\n"
            "  </definition>\n"
            "  <disabled>false</disabled>\n"
            "</flow-definition>\n"
        ).encode()

    def create_or_update_job(self, application_id: UUID, template_id: str) -> str:
        name = self.job_name(application_id)
        config_xml = self._job_config_xml(template_id)
        quoted = urllib.parse.quote(name)
        try:
            self._request(
                "POST",
                f"/createItem?name={quoted}&mode=org.jenkinsci.plugins.workflow.job.WorkflowJob",
                body=config_xml,
                content_type="application/xml",
            )
        except JenkinsHttpError as exc:
            if exc.status not in {400, 409}:
                raise
            # Already exists: reconcile it to the current template definition.
            self._request("POST", f"/job/{quoted}/config.xml", body=config_xml, content_type="application/xml")
        return name

    # --------------------------------------------------------------- triggering

    def trigger_ci_run(
        self, job_name: str, request: "CiLaunchRequest", callback_token: str = ""
    ) -> JenkinsRun:
        """Trigger a build and resolve the queue item into a real build identity."""

        query = urllib.parse.urlencode(
            {
                "NETCI_PIPELINE_RUN_ID": str(request.pipeline_run_id),
                "NETCI_CALLBACK_TOKEN": callback_token,
                "NETCI_APPLICATION_ID": str(request.application_id),
                "NETCI_CORRELATION_ID": request.correlation_id,
                "NETCI_API_URL": self.config.callback_url,
                "NETCI_ENVIRONMENT": request.environment,
                "NETCI_TEMPLATE": request.pipeline_template,
                "NETCI_STAGES": ",".join(request.stages),
                "GIT_URL": request.repository_url,
                "GIT_BRANCH": request.branch,
                "COMMIT_SHA": request.commit_sha,
                "NETCI_AGENT_LABEL": str(request.parameters.get("agentLabel", "")),
                "NETCI_BASE_IMAGE": self.config.base_image,
                "REGISTRY_PUSH_HOST": self.config.registry_push_host,
                "REGISTRY_PULL_HOST": self.config.registry_pull_host,
                "NETCI_TRIVY_DB_REPOSITORY": self.config.trivy_db_repository,
            }
        )
        _, headers, _ = self._request("POST", f"/job/{urllib.parse.quote(job_name)}/buildWithParameters?{query}")
        location = next((value for key, value in headers.items() if key.lower() == "location"), "")
        queue_id = location.rstrip("/").rsplit("/", 1)[-1] if location else ""
        if not queue_id.isdigit():
            return JenkinsRun(run_id=f"{job_name}@queue", status="queued")
        build = self.resolve_queue_item(queue_id)
        if build is None:
            return JenkinsRun(run_id=f"{job_name}@{queue_id}", status="queued")
        number, url = build
        return JenkinsRun(run_id=f"{job_name}#{number}", status="running", console_url=url)

    def resolve_queue_item(self, queue_id: str, *, attempts: int = 10, delay_seconds: float = 1.0) -> tuple[int, str] | None:
        """Poll a queue item until Jenkins assigns it a build number."""

        for attempt in range(attempts):
            try:
                _, _, body = self._request("GET", f"/queue/item/{urllib.parse.quote(queue_id)}/api/json", use_crumb=False)
            except JenkinsHttpError:
                return None
            payload: dict[str, Any] = json.loads(body or b"{}")
            if payload.get("cancelled"):
                raise JenkinsHttpError(409, f"Jenkins cancelled queue item {queue_id}")
            executable = payload.get("executable")
            if isinstance(executable, dict) and executable.get("number") is not None:
                return int(executable["number"]), str(executable.get("url") or "")
            if attempt < attempts - 1:
                time.sleep(delay_seconds)
        return None

    # ------------------------------------------------------------ run inspection

    @staticmethod
    def _build_path(run_id: str) -> str:
        job, _, number = run_id.partition("#")
        if not number:
            raise JenkinsHttpError(422, f"run id {run_id!r} has no resolved build number")
        return f"/job/{urllib.parse.quote(job)}/{urllib.parse.quote(number)}"

    def get_status(self, run_id: str) -> JenkinsRun:
        _, _, body = self._request("GET", f"{self._build_path(run_id)}/api/json", use_crumb=False)
        payload: dict[str, Any] = json.loads(body or b"{}")
        result = payload.get("result")
        if payload.get("building"):
            status = "running"
        elif result:
            status = {"SUCCESS": "succeeded", "FAILURE": "failed", "ABORTED": "cancelled", "UNSTABLE": "failed"}.get(
                str(result).upper(), str(result).lower()
            )
        else:
            status = "queued"
        return JenkinsRun(run_id=run_id, status=status, console_url=payload.get("url"))

    def get_logs(self, run_id: str) -> list[str]:
        _, _, body = self._request("GET", f"{self._build_path(run_id)}/consoleText", use_crumb=False)
        return body.decode(errors="replace").splitlines()

    def abort(self, run_id: str) -> None:
        self._request("POST", f"{self._build_path(run_id)}/stop")

    # `trigger_ci` keeps the narrow JenkinsAdapter protocol usable on its own.
    def trigger_ci(self, job_name: str, commit_sha: str, correlation_id: str) -> JenkinsRun:
        from .ci_launcher import CiLaunchRequest
        from uuid import uuid4

        return self.trigger_ci_run(
            job_name,
            CiLaunchRequest(
                application_id=uuid4(),
                application_name=job_name,
                repository_url="",
                pipeline_template="container-ci-cd-v1",
                runtime="docker",
                stages=(),
                pipeline_run_id=uuid4(),
                commit_sha=commit_sha,
                branch="main",
                environment="dev",
                correlation_id=correlation_id,
                parameters={},
            ),
        )
