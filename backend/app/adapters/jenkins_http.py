from __future__ import annotations

import base64
import http.cookiejar
import hashlib
import re
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
    # Custom catalog stages as JSON: [{id, name, script, after}]. The script is a path in
    # the checked-out repository; the pipeline runs it after the anchor stage.
    "NETCI_CUSTOM_STAGES",
    "GIT_URL",
    "GIT_BRANCH",
    "COMMIT_SHA",
    # What to build and what to call it. Neither reached Jenkins before, so the CI scripts
    # fell back to their defaults -- sample-apps/hello-container, image `hello-container`
    # -- and every container module in the lab built and pushed the same sample app under
    # that one name, whatever its own repository held.
    "NETCI_APP_DIR",
    "NETCI_IMAGE_NAME",
    # "false" for a verify-only build: no Sign, no Publish, no evidence (ADR-043).
    "NETCI_PUBLISH",
    # The pull request head ref to fetch when the commit is on no branch (a fork's).
    "NETCI_GIT_REF",
    # Lets the same job be sent to a shared or an ephemeral agent, which is what the
    # benchmark compares. Empty means "use the template default".
    "NETCI_AGENT_LABEL",
    # Per-project isolation (ADR-030): the namespace the pod is created in, the service
    # account it runs as, and the claim that carries the project's warm cache.
    "NETCI_BUILD_NAMESPACE",
    "NETCI_BUILD_SERVICE_ACCOUNT",
    "NETCI_BUILD_CACHE_CLAIM",
    "NETCI_BASE_IMAGE",
    "REGISTRY_PUSH_HOST",
    "REGISTRY_PULL_HOST",
    "NETCI_TRIVY_DB_REPOSITORY",
)


#: The library's stage list when netCI sends none (netciPipeline.groovy `defaultStages`).
LIBRARY_DEFAULT_STAGES: tuple[str, ...] = (
    "checkout", "unit-test", "build", "sbom", "vulnerability-scan", "sign", "publish",
)
UNPUBLISHED_STAGES = frozenset({"sign", "publish"})


def _stages_for(request: "CiLaunchRequest") -> list[str]:
    """The stage list Jenkins runs. A verify-only build names none that sign or publish.

    `NETCI_PUBLISH=false` is what the current library reads. This is what a controller
    still on an older library reads: it has honoured `NETCI_STAGES` since the start, so
    the signing key stays out of a fork's build even there. Such a build then fails at
    Publish Evidence for want of a digest -- a visible failure, not a leaked key.
    """

    stages = list(request.stages) or list(LIBRARY_DEFAULT_STAGES)
    if not request.publish_artifact:
        stages = [stage for stage in stages if stage not in UNPUBLISHED_STAGES]
    return stages


def image_name_for(application_name: str) -> str:
    """An OCI repository name component for an application: lowercase, [a-z0-9._-].

    Application names are already slugs, so this normally returns them unchanged; it is
    here so a name that is not one cannot produce a reference the registry rejects late,
    after a build has spent its time.
    """

    name = re.sub(r"[^a-z0-9._-]+", "-", application_name.strip().lower()).strip("._-")
    if not name:
        raise ValueError(f"application name {application_name!r} has no usable image name")
    return name[:128]


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

    @staticmethod
    def _normalize_jcasc(exported: str) -> str:
        normalized = re.sub(r"\{AQAAAB[A-Za-z0-9+/=]+\}", "{REDACTED}", exported)
        normalized = re.sub(
            r'id: "[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"', 'id: "{GENERATED}"', normalized
        )
        # Controller identity is expected to differ; it is not drift.
        normalized = re.sub(r'value: "jenkins-[a-z0-9-]+"', 'value: "{CONTROLLER}"', normalized)
        normalized = re.sub(r'jenkinsUrl: "[^"]+"', 'jenkinsUrl: "{CONTROLLER}"', normalized)
        normalized = re.sub(r'systemMessage: "[^"]*"', 'systemMessage: "{CONTROLLER}"', normalized)
        # Runtime state the export carries along: label atoms of pods that happen to be
        # running, and a library path the plugin writes once a library has been loaded.
        normalized = re.sub(r'\n  - name: "[^"]+-[a-z0-9]{5}-[a-z0-9]{5}(?:-[a-z0-9]{5})?"', "", normalized)
        normalized = re.sub(r'\n  - name: "[^"]+_[0-9]+-[a-z0-9]{5}"', "", normalized)
        normalized = re.sub(r'\n *libraryPath: "\."', "", normalized)
        return normalized

    def configuration_fingerprint(self) -> dict[str, object]:
        """What this controller is actually running, in a form two controllers can be
        compared by: the live JCasC export (per-instance ciphertext and generated ids
        masked), the plugin set and the job list. Drift between controllers is what
        makes a build behave differently depending on where the router sent it.
        """

        status, _, body = self._request("POST", "/manage/configuration-as-code/export", body=b"", content_type="text/plain")
        if not 200 <= status < 300:
            raise JenkinsHttpError(f"JCasC export returned {status}")
        exported = body.decode(errors="replace")
        normalized = self._normalize_jcasc(exported)
        _, _, plugins_body = self._request("GET", "/pluginManager/api/json?depth=1&tree=plugins%5BshortName,version%5D", use_crumb=False)
        plugins = sorted(
            f"{item['shortName']}@{item['version']}" for item in json.loads(plugins_body or b"{}").get("plugins", [])
        )
        _, _, jobs_body = self._request("GET", "/api/json?tree=jobs%5Bname%5D", use_crumb=False)
        jobs = sorted(str(item["name"]) for item in json.loads(jobs_body or b"{}").get("jobs", []))
        return {
            "jcascNormalizedSha256": hashlib.sha256(normalized.encode()).hexdigest(),
            "pluginsSha256": hashlib.sha256("\n".join(plugins).encode()).hexdigest(),
            "pluginCount": len(plugins),
            "jobs": jobs,
        }

    def reload_configuration(self) -> None:
        """Make the controller re-read its JCasC sources (files and /run/secrets).

        This is how a change pushed to jenkins/casc, or a rotated secret file, reaches a
        running controller without a rebuild (ADR-033). Jenkins answers 200 on success
        and 4xx/5xx when the configuration does not apply; either way the caller
        compares the controllers afterwards.
        """

        status, _, body = self._request("POST", "/configuration-as-code/reload", body=b"", content_type="text/plain")
        if not 200 <= status < 300:
            raise JenkinsHttpError(f"JCasC reload returned {status}: {body.decode(errors='replace')[:200]}")

    def queued_builds(self) -> int | None:
        """Builds waiting in this controller's own queue, or None when it cannot say.

        None, not 0: admission reads this as "is Jenkins already saturated" (ADR-050), and
        an unreachable queue taken for an empty one would pile builds onto a controller
        that is not keeping up.
        """

        try:
            status, _, body = self._request("GET", "/queue/api/json?tree=items[id]", use_crumb=False)
            if not 200 <= status < 300:
                return None
            items = json.loads(body or b"{}").get("items")
            return len(items) if isinstance(items, list) else None
        except (JenkinsHttpError, OSError, ValueError):
            return None

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
                "NETCI_STAGES": ",".join(_stages_for(request)),
                "NETCI_CUSTOM_STAGES": json.dumps(request.custom_stages) if request.custom_stages else "",
                "GIT_URL": request.repository_url,
                "GIT_BRANCH": request.branch,
                "COMMIT_SHA": request.commit_sha,
                # A build input, validated by build_inputs.application_directory; the
                # repository root unless the module lives in a subdirectory.
                "NETCI_APP_DIR": str(request.parameters.get("NETCI_APP_DIR") or "."),
                # Decided here, not by the caller: the image repository is where the
                # artifact lands, and one module must not be able to publish as another.
                "NETCI_IMAGE_NAME": image_name_for(request.application_name),
                "NETCI_PUBLISH": "true" if request.publish_artifact else "false",
                "NETCI_GIT_REF": request.source_ref,
                "NETCI_AGENT_LABEL": str(request.parameters.get("agentLabel", "")),
                **(request.isolation.as_parameters() if request.isolation else {
                    "NETCI_BUILD_NAMESPACE": "", "NETCI_BUILD_SERVICE_ACCOUNT": "", "NETCI_BUILD_CACHE_CLAIM": "",
                }),
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
