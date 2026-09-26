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
from .signature_verifier import rekor_url_setting

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
    # The registry path every artifact is published under ("apps" -> <host>/apps/<image>):
    # a registry like Harbor keeps repositories in projects with their own permissions, and
    # the build's credential may push to one project only. Server-owned, like the image name.
    registry_namespace: str = ""
    base_image: str = ""
    # Where Trivy fetches its vulnerability database. A build farm without internet
    # access needs a mirror; a stale or missing database would silently weaken the scan.
    trivy_db_repository: str = ""
    cosign_credentials_id: str = "netci-cosign-key"
    # A Jenkins folder ("platform/netci") netCI's jobs live in. Empty means the root.
    folder: str = ""
    # Optional Jenkins credentials for a private git server and an authenticated registry
    # (ADR-054). Empty is anonymous, which is what the lab's git server and registry are.
    # They name credentials on the controller; netCI never holds the secrets themselves.
    git_credentials_id: str = ""
    registry_credentials_id: str = ""
    # Secret text holding the cosign key's password. Empty is a key with no password.
    cosign_password_credentials_id: str = ""
    # What the build is told about TLS and the transparency log. Left to the scripts'
    # defaults, every build ran with `REGISTRY_TLS_VERIFY=false`, cosign's
    # --allow-insecure-registry and no tlog upload, whatever netCI itself demanded -- and
    # a signature netCI requires to be in the tlog could never verify.
    registry_allow_http: bool = False
    signature_require_tlog: bool = False
    #: The transparency log to upload to when the tlog is required. Never defaulted: an
    #: unset one would be the public Rekor, publishing every internal image digest.
    rekor_url: str = ""

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
            registry_namespace=registry_namespace_setting(os.getenv("NETCI_REGISTRY_NAMESPACE", "")),
            base_image=os.getenv("NETCI_BUILD_BASE_IMAGE", ""),
            trivy_db_repository=os.getenv("NETCI_TRIVY_DB_REPOSITORY", ""),
            cosign_credentials_id=_credentials_id(
                "NETCI_COSIGN_CREDENTIALS_ID", os.getenv("NETCI_COSIGN_CREDENTIALS_ID", "netci-cosign-key")),
            git_credentials_id=_credentials_id(
                "NETCI_GIT_CREDENTIALS_ID", os.getenv("NETCI_GIT_CREDENTIALS_ID", "")),
            registry_credentials_id=_credentials_id(
                "NETCI_REGISTRY_CREDENTIALS_ID", os.getenv("NETCI_REGISTRY_CREDENTIALS_ID", "")),
            cosign_password_credentials_id=_credentials_id(
                "NETCI_COSIGN_PASSWORD_CREDENTIALS_ID", os.getenv("NETCI_COSIGN_PASSWORD_CREDENTIALS_ID", "")),
            # Read the way the worker reads NETCI_REGISTRY_ALLOW_HTTP and the signature
            # verifier reads NETCI_SIGNATURE_REQUIRE_TLOG, so the build and netCI agree.
            registry_allow_http=os.getenv("NETCI_REGISTRY_ALLOW_HTTP", "").strip().lower() in {"1", "true", "yes"},
            signature_require_tlog=os.getenv("NETCI_SIGNATURE_REQUIRE_TLOG", "false").strip().lower()
            in {"1", "true", "yes"},
            rekor_url=rekor_url_setting(),
            folder=jenkins_folder(os.getenv("NETCI_JENKINS_FOLDER", "")),
        )


#: OCI repository path components (distribution spec), joined by "/".
_REGISTRY_NAMESPACE = re.compile(r"[a-z0-9]+(?:[._-][a-z0-9]+)*(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)*")


def registry_namespace_setting(value: str) -> str:
    """NETCI_REGISTRY_NAMESPACE, refused at startup unless it is a valid repository path.

    A bad value would otherwise surface only when the first build tries to push -- after
    it has spent its time -- or, worse, publish somewhere nobody meant.
    """

    value = value.strip().strip("/")
    if value and not _REGISTRY_NAMESPACE.fullmatch(value):
        raise ValueError(f"NETCI_REGISTRY_NAMESPACE {value!r} is not a registry path (e.g. 'apps' or 'org/apps')")
    return value


#: What Jenkins accepts as a credential id (BaseStandardCredentials' id check).
_CREDENTIALS_ID = re.compile(r"[A-Za-z0-9_.-]{0,256}")


def _credentials_id(name: str, value: str) -> str:
    """A credential id from the environment, refused unless Jenkins could hold it.

    The id is written into the job's Groovy as a string literal. One Jenkins would never
    accept as an id is a mistake or an injection; either way the job would not bind what
    was configured, so it stops startup instead.
    """

    value = value.strip()
    if not _CREDENTIALS_ID.fullmatch(value):
        raise ValueError(f"{name} is not a Jenkins credential id (letters, digits, '_', '.', '-')")
    return value


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
    "NETCI_IMAGE_NAMESPACE",
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
    # "true" unless netCI is configured for a plain-HTTP registry; cosign's
    # --allow-insecure-registry follows it (ADR-054).
    "REGISTRY_TLS_VERIFY",
    # "true" when netCI requires a transparency-log entry to accept a signature.
    "COSIGN_TLOG_UPLOAD",
    "COSIGN_REKOR_URL",
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


def _registry_parameters(config: JenkinsHttpConfig) -> dict[str, str]:
    """Registry TLS and transparency log, sent with every build rather than left to defaults.

    The scripts keep their old insecure defaults for a controller an older netCI drives.
    A build this netCI dispatches is always told, so it checks what netCI checks.
    """

    if config.signature_require_tlog and not config.rekor_url:
        raise ValueError("NETCI_SIGNATURE_REQUIRE_TLOG=true needs NETCI_REKOR_URL: the build would otherwise "
                         "upload to the public Rekor")
    return {
        "REGISTRY_TLS_VERIFY": "false" if config.registry_allow_http else "true",
        "COSIGN_TLOG_UPLOAD": "true" if config.signature_require_tlog else "false",
        "COSIGN_REKOR_URL": config.rekor_url if config.signature_require_tlog else "",
    }



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


_FOLDER_SEGMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")


def jenkins_folder(value: str) -> str:
    """Validate a folder path ("platform/netci") and return it normalised.

    Refused rather than quoted: a segment is spliced into every job URL, and a name like
    `..` or one holding `?`/`#` would address something other than the folder meant. A
    company that uses other characters in folder names can rename or alias the folder;
    guessing at an encoding here could send builds to the wrong place.
    """

    folder = value.strip().strip("/")
    if not folder:
        return ""
    for segment in folder.split("/"):
        if not _FOLDER_SEGMENT.fullmatch(segment):
            raise ValueError(
                f"NETCI_JENKINS_FOLDER segment {segment!r} is not a plain folder name "
                "(letters, digits, '.', '_', '-'; not starting with a separator)"
            )
    return folder


# A run id names a build on one controller. Resolved: `<job>#<number>`. Not yet resolved
# -- still in Jenkins' queue when the trigger stopped waiting, which is the normal case
# on a controller with a quiet period and busy agents: `<job>@q<queueId>/<pipelineRunId>`.
# The queue id is how Jenkins itself links the queue item to its build; the pipeline run
# id is the NETCI_PIPELINE_RUN_ID parameter the build carries, which is what still finds
# it after Jenkins has forgotten the queue item (about five minutes after it started).
# `<job>@<queueId>` and `<job>@queue` are the forms stored before this, still parsed.
_RESOLVED_RUN = re.compile(r"(?P<job>[^#@/]+)#(?P<number>\d+)")
_QUEUED_RUN = re.compile(r"(?P<job>[^#@/]+)@q(?P<queue>\d*)/(?P<run>[0-9a-fA-F-]{36})")
_LEGACY_QUEUED_RUN = re.compile(r"(?P<job>[^#@/]+)@(?P<queue>\d+|queue)")

_BUILD_STATUS = {"SUCCESS": "succeeded", "FAILURE": "failed", "ABORTED": "cancelled", "UNSTABLE": "failed"}


@dataclass(frozen=True)
class _RunRef:
    job: str
    number: int | None = None
    queue_id: str = ""
    pipeline_run_id: str = ""


def queued_run_id(job_name: str, queue_id: str, pipeline_run_id: str) -> str:
    return f"{job_name}@q{queue_id}/{pipeline_run_id}"


def _parse_run_id(run_id: str) -> _RunRef:
    if match := _RESOLVED_RUN.fullmatch(run_id):
        return _RunRef(job=match["job"], number=int(match["number"]))
    if match := _QUEUED_RUN.fullmatch(run_id):
        return _RunRef(job=match["job"], queue_id=match["queue"], pipeline_run_id=match["run"].lower())
    if match := _LEGACY_QUEUED_RUN.fullmatch(run_id):
        return _RunRef(job=match["job"], queue_id="" if match["queue"] == "queue" else match["queue"])
    raise JenkinsHttpError(422, f"run id {run_id!r} is not a Jenkins run id netCI issued")


def _jenkins_message(detail: str, limit: int = 300) -> str:
    """Jenkins' error text without the page chrome, short enough for a log line."""

    text = re.sub(r"<[^>]+>", " ", detail)
    return re.sub(r"\s+", " ", text).strip()[:limit]


class JenkinsHttpAdapter:
    """Jenkins REST adapter.

    Jobs are managed as **pipeline** jobs (`flow-definition` + `CpsFlowDefinition`)
    that load the `netciPipeline` shared library, so a build's stage graph comes from
    Git rather than from clicked-in freestyle steps.  No Jenkins detail leaks into the
    domain: callers see `JenkinsRun`.
    """

    def __init__(self, config: JenkinsHttpConfig) -> None:
        self.config = config
        # Validated here as well as in from_env: a config built in code must not be able
        # to splice an unchecked segment into job URLs either.
        self.folder = jenkins_folder(config.folder)
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

    def _folder_path(self) -> str:
        """`/job/platform/job/netci` for folder `platform/netci`; empty at the root."""

        return "".join(f"/job/{urllib.parse.quote(segment)}" for segment in self.folder.split("/") if segment)

    def _job_path(self, job_name: str) -> str:
        return f"{self._folder_path()}/job/{urllib.parse.quote(job_name)}"

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

        # The plugin's own path, like reload below. `/manage/configuration-as-code/` is
        # the same page reached through the Manage Jenkins alias of newer cores; using one
        # form for both calls keeps a proxy allow-list or a permission probe to one path.
        status, _, body = self._request("POST", "/configuration-as-code/export", body=b"", content_type="text/plain")
        if not 200 <= status < 300:
            raise JenkinsHttpError(status, f"JCasC export returned {status}")
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
            raise JenkinsHttpError(status, f"JCasC reload returned {status}: {_jenkins_message(body.decode(errors='replace'), 200)}")

    def queued_builds(self) -> int | None:
        """Builds waiting in this controller's own queue, or None when it cannot say.

        None, not 0: admission reads this as "is Jenkins already saturated" (ADR-050), and
        an unreachable queue taken for an empty one would pile builds onto a controller
        that is not keeping up.
        """

        # Only netCI's own items. On a controller other teams share, their queued builds
        # would otherwise count against netCI's budget and netCI would admit nothing.
        try:
            status, _, body = self._request("GET", "/queue/api/json?tree=items[id,task[name,url]]", use_crumb=False)
            if not 200 <= status < 300:
                return None
            items = json.loads(body or b"{}").get("items")
            if not isinstance(items, list):
                return None
            return sum(1 for item in items if isinstance(item, dict) and self._is_netci_task(item.get("task")))
        except (JenkinsHttpError, OSError, ValueError):
            return None

    def _is_netci_task(self, task: object) -> bool:
        if not isinstance(task, dict):
            return False
        name = str(task.get("name") or "")
        if not name.startswith("netci-"):
            return False
        if not self.folder:
            return True
        # Compared by path suffix: the task url carries Jenkins' configured root URL,
        # which need not be the address netCI calls it by (a context path, a proxy).
        path = urllib.parse.urlparse(str(task.get("url") or "")).path.rstrip("/")
        return path.endswith(self._job_path(name))

    def queue_depth(self) -> int:
        return self.queued_builds() or 0

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
            f"    cosignCredentialsId: '{self.config.cosign_credentials_id}',\n"
            # Empty is an unencrypted key, anonymous git, an anonymous registry (ADR-054).
            f"    cosignPasswordCredentialsId: '{self.config.cosign_password_credentials_id}',\n"
            f"    gitCredentialsId: '{self.config.git_credentials_id}',\n"
            f"    registryCredentialsId: '{self.config.registry_credentials_id}'\n"
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
                f"{self._folder_path()}/createItem?name={quoted}&mode=org.jenkinsci.plugins.workflow.job.WorkflowJob",
                body=config_xml,
                content_type="application/xml",
            )
        except JenkinsHttpError as exc:
            if exc.status == 404 and self.folder:
                # netCI never creates the folder: where its jobs live, and who may reach
                # them, is the Jenkins owners' decision, made once, by hand.
                raise JenkinsHttpError(
                    404,
                    f"Jenkins folder {self.folder!r} does not exist, or the service account cannot "
                    "see it; netCI does not create it -- ask the Jenkins owners to create it and "
                    "grant the account Job/Create inside it",
                ) from exc
            if exc.status != 409 and not (exc.status == 400 and self._job_exists(name)):
                # Any other refusal -- a bad name, a missing plugin behind the job type, a
                # rejected config -- was once taken for "already exists" and answered by
                # overwriting config.xml, which hid the real reason until the first build.
                raise JenkinsHttpError(
                    exc.status, f"Jenkins refused to create job {name}: {_jenkins_message(exc.message)}"
                ) from exc
            # Already exists: reconcile it to the current template definition.
            self._request("POST", f"{self._job_path(name)}/config.xml", body=config_xml, content_type="application/xml")
        return name

    def _job_exists(self, name: str) -> bool:
        # Asked, not read from the 400 page: Jenkins words "already exists" in its own
        # locale and inside a full HTML page, so the text is no contract.
        try:
            status, _, _ = self._request("GET", f"{self._job_path(name)}/api/json?tree=name", use_crumb=False)
        except JenkinsHttpError:
            return False
        return 200 <= status < 300

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
                "NETCI_APP_DIR": str(
                    request.parameters.get("NETCI_APP_DIR")
                    or (
                        f"sample-apps/{request.application_name}"
                        if ("netci.git" in request.repository_url or "sample-apps" in request.repository_url)
                        else "."
                    )
                ),
                # Decided here, not by the caller: the image repository is where the
                # artifact lands, and one module must not be able to publish as another.
                "NETCI_IMAGE_NAME": image_name_for(request.application_name),
                "NETCI_IMAGE_NAMESPACE": self.config.registry_namespace,
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
                **_registry_parameters(self.config),
            }
        )
        # `query` is sent as the form body, never in the URL: it carries the callback
        # token, and a URL lands in Jenkins' and every proxy's access log -- and, through
        # JenkinsHttpError, in netCI's own launch-failure log and audit record. Jenkins
        # reads buildWithParameters' values from a form-encoded POST body.
        _, headers, _ = self._request(
            "POST",
            f"{self._job_path(job_name)}/buildWithParameters",
            body=query.encode(),
            content_type="application/x-www-form-urlencoded",
        )
        location = next((value for key, value in headers.items() if key.lower() == "location"), "")
        queue_id = location.rstrip("/").rsplit("/", 1)[-1] if location else ""
        if not queue_id.isdigit():
            queue_id = ""
        build = self.resolve_queue_item(queue_id) if queue_id else None
        if build is None:
            # Still waiting (quiet period, no free agent). The id keeps what is needed to
            # find the build later; get_status and abort resolve it on every call.
            return JenkinsRun(run_id=queued_run_id(job_name, queue_id, str(request.pipeline_run_id)), status="queued")
        number, url = build
        return JenkinsRun(run_id=f"{job_name}#{number}", status="running", console_url=url)

    def _queue_item(self, queue_id: str) -> dict[str, Any] | None:
        """The queue item, or None once Jenkins has forgotten it (404)."""

        try:
            _, _, body = self._request("GET", f"/queue/item/{urllib.parse.quote(queue_id)}/api/json", use_crumb=False)
        except JenkinsHttpError as exc:
            if exc.status == 404:
                return None
            raise
        payload = json.loads(body or b"{}")
        return payload if isinstance(payload, dict) else {}

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

    def _locate(self, ref: _RunRef) -> tuple[str, int | None]:
        """Where a run is now: ("build", number), ("queued", None) or ("cancelled", None).

        Raises JenkinsHttpError(404) when neither the queue nor the job's recent builds
        know it -- "unknown", which the caller must not read as any status.
        """

        if ref.number is not None:
            return "build", ref.number
        if ref.queue_id:
            item = self._queue_item(ref.queue_id)
            if item is not None:
                if item.get("cancelled"):
                    return "cancelled", None
                executable = item.get("executable")
                if isinstance(executable, dict) and executable.get("number") is not None:
                    return "build", int(executable["number"])
                return "queued", None
        # Jenkins keeps a left queue item for about five minutes; after that only the
        # build itself says which run it was.
        return "build", self._find_build(ref)

    def _find_build(self, ref: _RunRef) -> int:
        if not ref.pipeline_run_id and not ref.queue_id:
            raise JenkinsHttpError(422, f"run of {ref.job} has neither a queue id nor a pipeline run id to find it by")
        tree = urllib.parse.quote("builds[number,queueId,actions[parameters[name,value]]]{0,50}", safe=",")
        _, _, body = self._request("GET", f"{self._job_path(ref.job)}/api/json?tree={tree}", use_crumb=False)
        builds = json.loads(body or b"{}").get("builds") or []
        by_queue: list[int] = []
        by_parameter: list[int] = []
        for build in builds:
            if not isinstance(build, dict) or build.get("number") is None:
                continue
            number = int(build["number"])
            if ref.queue_id and str(build.get("queueId")) == ref.queue_id:
                by_queue.append(number)
            if ref.pipeline_run_id and any(
                isinstance(parameter, dict)
                and parameter.get("name") == "NETCI_PIPELINE_RUN_ID"
                and str(parameter.get("value") or "").lower() == ref.pipeline_run_id
                for action in build.get("actions") or [] if isinstance(action, dict)
                for parameter in action.get("parameters") or []
            ):
                by_parameter.append(number)
        if ref.pipeline_run_id:
            # The parameter is the identity; the queue id only narrows a double trigger.
            both = [number for number in by_parameter if number in by_queue]
            candidates = both or by_parameter
        else:
            # A run id stored before the pipeline run id was part of it.
            candidates = by_queue
        if len(candidates) == 1:
            return candidates[0]
        if not candidates:
            raise JenkinsHttpError(404, f"no recent build of {ref.job} belongs to this run")
        raise JenkinsHttpError(409, f"builds {sorted(candidates)} of {ref.job} all claim this run")

    def _build_path(self, job: str, number: int) -> str:
        return f"{self._job_path(job)}/{number}"

    def get_status(self, run_id: str) -> JenkinsRun:
        """The run's status now, resolving a queued run id on every call.

        The returned `run_id` is the resolved `<job>#<n>` once a build exists, so a caller
        that can store it may; one that cannot loses nothing, because the queued form is
        resolved again next time.
        """

        ref = _parse_run_id(run_id)
        where, number = self._locate(ref)
        if where != "build" or number is None:
            return JenkinsRun(run_id=run_id, status=where)
        _, _, body = self._request("GET", f"{self._build_path(ref.job, number)}/api/json", use_crumb=False)
        payload: dict[str, Any] = json.loads(body or b"{}")
        result = payload.get("result")
        if payload.get("building"):
            status = "running"
        elif result:
            status = _BUILD_STATUS.get(str(result).upper(), str(result).lower())
        else:
            status = "queued"
        return JenkinsRun(run_id=f"{ref.job}#{number}", status=status, console_url=payload.get("url"))

    def get_logs(self, run_id: str) -> list[str]:
        ref = _parse_run_id(run_id)
        where, number = self._locate(ref)
        if where != "build" or number is None:
            return []  # nothing has run, so there is no console yet
        _, _, body = self._request("GET", f"{self._build_path(ref.job, number)}/consoleText", use_crumb=False)
        return body.decode(errors="replace").splitlines()

    def abort(self, run_id: str) -> None:
        ref = _parse_run_id(run_id)
        where, number = self._locate(ref)
        if where == "cancelled":
            return
        if where == "queued":
            refused: JenkinsHttpError | None = None
            try:
                self._request("POST", f"/queue/cancelItem?id={urllib.parse.quote(ref.queue_id)}")
            except JenkinsHttpError as exc:
                refused = exc
            # Read back rather than trusting the answer: the item can leave the queue for
            # an executor between the read above and the cancel, and cancelItem then does
            # nothing. Some cores also answer a successful cancel with a 404.
            where, number = self._locate(ref)
            if where == "cancelled":
                return
            if where == "queued":
                raise refused or JenkinsHttpError(409, f"Jenkins kept queue item {ref.queue_id} after cancelItem")
        if number is None:
            raise JenkinsHttpError(404, f"no build of {ref.job} to stop")
        self._request("POST", f"{self._build_path(ref.job, number)}/stop")

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
