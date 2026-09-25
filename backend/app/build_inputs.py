"""What a caller may put into a pipeline run, and what only the server decides.

`PipelineRunCreate.parameters` used to be `dict[str, object]`: an open map passed
straight through the domain into the CD orchestrator, where the runtime adapters read
keys like `target_hosts`, `kubeconfig_ref` and `artifact_url` out of it. A browser could
therefore choose which machines a deployment touched, which namespace it wrote to, which
credential it used and which artifact it installed -- by adding a key to a JSON body.

Three kinds of value get confused in one map, and separating them is the fix:

* **Build inputs** are the caller's business: a feature flag for the build, a tag suffix,
  a log level. They are strings, numbers and booleans, allow-listed by name, bounded in
  size and depth, and they never decide where anything is deployed.
* **Application configuration** is immutable at run time -- runtime, template, repository.
  It came from onboarding, and a run cannot change it.
* **Deployment parameters** are server-managed: target hosts, namespace, credential
  references, artifact digest and URL, playbook, health command, rollback behaviour.
  These are computed from the module's registered configuration by
  `PortalService.delivery_parameters`, and they are applied *after* any caller input, so
  a caller cannot shadow one even by guessing its name.

The list below is the trust boundary written down. A key on it is refused with `422`
rather than silently dropped: silently dropping teaches a caller that its override
worked, and the operator finds out at the incident review.
"""

from __future__ import annotations

import os
import re
from typing import Any

#: Keys that decide where or how something is deployed. A caller naming one of these is
#: refused, whether or not it would have taken effect, because the attempt is the signal.
DEPLOYMENT_CONTROLLED_KEYS: frozenset[str] = frozenset(
    {
        # Where it lands
        "target_hosts", "targethosts", "hosts", "host", "inventory", "inventory_file",
        "target_environment", "environment", "target_namespace", "namespace",
        "cluster", "kubecontext", "kube_context",
        # What credential it uses
        "kubeconfig", "kubeconfig_ref", "kubeconfigref", "credential", "credentials",
        "credential_ref", "credentials_id", "credentialsid", "ssh_key", "ssh_private_key",
        "private_key", "token", "password", "secret", "api_key", "apikey",
        "registry_password", "registry_username", "vault_token",
        # What it installs
        "artifact_url", "artifacturl", "artifact_ref", "artifactref", "artifact_digest",
        "artifactdigest", "artifact_sha256", "image", "image_repository", "image_tag",
        "image_digest", "registry", "registry_url",
        # What it runs
        "playbook", "playbook_path", "command", "cmd", "script", "shell", "entrypoint",
        "health_command", "healthcheck", "health_check", "post_deploy", "pre_deploy",
        "deployment_tasks", "task_settings", "tasks",
        # How traffic reaches it (ADR-031): the release track and canary weight are the
        # production request's, applied by the coordinator, never a build input.
        "release_track", "releasetrack", "canary_weight", "canaryweight", "canary",
        # How it recovers
        "rollback", "rollback_strategy", "rollback_command", "runtime_health_verified",
        "require_approval", "approved_by", "actor", "owner", "owner_team", "app_name",
        # Post-deploy verification is the module's (ADR-046); a caller must not be able
        # to replace the thresholds or the queries for one run.
        "verification",
    }
)

#: Build inputs a caller may set. Adding one here is a deliberate act: it must be a value
#: that cannot change the deployment target, the credential or the artifact.
ALLOWED_BUILD_INPUT_KEYS: frozenset[str] = frozenset(
    {
        "buildArgs",         # object of scalars, passed to the builder as --build-arg
        "buildProfile",      # named profile the pipeline template understands
        "skipTests",         # boolean; the security gate still applies
        "logLevel",
        "notes",
        "commitTimestamp",   # used for the DORA lead-time origin
        "sourceBranchRef",
        "portalPipeline",    # frontend portal UI pipeline identifier (e.g. ci, cd-dev, cd-staging, cd-prod)
        "NETCI_APP_DIR",     # relative path to the application directory in monorepo
        "agentLabel",        # Jenkins agent label override (e.g. ephemeral vs shared baseline)
    }
)

MAX_INPUT_KEYS = 32
MAX_NESTED_KEYS = 32
MAX_STRING_LENGTH = 1024
MAX_TOTAL_BYTES = 16 * 1024
MAX_DEPTH = 2


class BuildInputError(ValueError):
    """A caller-supplied parameter was not acceptable. Always a 422."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _check_scalar(key: str, value: Any) -> None:
    if isinstance(value, bool) or isinstance(value, int) or isinstance(value, float):
        return
    if isinstance(value, str):
        if len(value) > MAX_STRING_LENGTH:
            raise BuildInputError(
                "BUILD_INPUT_TOO_LARGE",
                f"parameter {key!r} exceeds {MAX_STRING_LENGTH} characters",
            )
        return
    raise BuildInputError(
        "BUILD_INPUT_TYPE_NOT_ALLOWED",
        f"parameter {key!r} must be a string, number or boolean",
    )


def validate_build_inputs(supplied: dict[str, Any] | None) -> dict[str, Any]:
    """Return the caller's build inputs, or raise `BuildInputError`.

    Refusing is the whole point. A caller that names a deployment-controlled key is told
    so, because the alternative -- dropping it -- looks identical to it having worked.
    """

    if not supplied:
        return {}
    if not isinstance(supplied, dict):
        raise BuildInputError("BUILD_INPUT_INVALID", "parameters must be an object")
    if len(supplied) > MAX_INPUT_KEYS:
        raise BuildInputError(
            "BUILD_INPUT_TOO_MANY", f"at most {MAX_INPUT_KEYS} parameters are accepted"
        )

    cleaned: dict[str, Any] = {}
    for key, value in supplied.items():
        if not isinstance(key, str):
            raise BuildInputError("BUILD_INPUT_INVALID", "parameter names must be strings")
        normalized = key.strip().lower().replace("-", "_")
        if normalized in DEPLOYMENT_CONTROLLED_KEYS:
            raise BuildInputError(
                "DEPLOYMENT_PARAMETER_NOT_ACCEPTED",
                f"{key!r} is decided by the module's registered configuration and cannot be "
                "supplied with a run",
            )
        if key not in ALLOWED_BUILD_INPUT_KEYS:
            raise BuildInputError(
                "BUILD_INPUT_NOT_ALLOWED",
                f"unknown parameter {key!r}; allowed build inputs are: "
                + ", ".join(sorted(ALLOWED_BUILD_INPUT_KEYS)),
            )
        if isinstance(value, dict):
            if len(value) > MAX_NESTED_KEYS:
                raise BuildInputError(
                    "BUILD_INPUT_TOO_MANY",
                    f"parameter {key!r} has more than {MAX_NESTED_KEYS} entries",
                )
            for nested_key, nested_value in value.items():
                if not isinstance(nested_key, str):
                    raise BuildInputError(
                        "BUILD_INPUT_INVALID", f"keys inside {key!r} must be strings"
                    )
                if nested_key.strip().lower().replace("-", "_") in DEPLOYMENT_CONTROLLED_KEYS:
                    raise BuildInputError(
                        "DEPLOYMENT_PARAMETER_NOT_ACCEPTED",
                        f"{nested_key!r} inside {key!r} is decided by the module's registered "
                        "configuration and cannot be supplied with a run",
                    )
                if isinstance(nested_value, (dict, list)):
                    raise BuildInputError(
                        "BUILD_INPUT_TOO_DEEP",
                        f"parameter {key!r} may nest at most {MAX_DEPTH} levels",
                    )
                _check_scalar(f"{key}.{nested_key}", nested_value)
        elif isinstance(value, list):
            raise BuildInputError(
                "BUILD_INPUT_TYPE_NOT_ALLOWED",
                f"parameter {key!r} must be a string, number, boolean or object of scalars",
            )
        else:
            _check_scalar(key, value)
        if key == "NETCI_APP_DIR":
            value = application_directory(value)
        if key == "agentLabel":
            value = agent_label(value)
        cleaned[key] = value

    total = _approximate_size(cleaned)
    if total > MAX_TOTAL_BYTES:
        raise BuildInputError(
            "BUILD_INPUT_TOO_LARGE",
            f"parameters exceed {MAX_TOTAL_BYTES} bytes in total",
        )
    return cleaned


_AGENT_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$")


def agent_label(value: Any) -> str:
    """A pod template the platform lets a run choose, or a refusal.

    The label is the Kubernetes pod template the build inherits (`inheritFrom`). Taken
    as given, a caller could pick any template on the controller -- a reusable one that
    keeps its workspace between builds, one with another service account -- and step
    out of the one-pod-per-build isolation (ADR-007). So only labels the operator listed
    in NETCI_ALLOWED_AGENT_LABELS are accepted; with none listed, choosing is refused.
    """

    if not isinstance(value, str) or not _AGENT_LABEL.fullmatch(value.strip()):
        raise BuildInputError("BUILD_INPUT_INVALID", "agentLabel must be a pod template label")
    allowed = {item.strip() for item in os.getenv("NETCI_ALLOWED_AGENT_LABELS", "").split(",") if item.strip()}
    label = value.strip()
    if label not in allowed:
        raise BuildInputError(
            "AGENT_LABEL_NOT_ALLOWED",
            f"agentLabel {label!r} is not one the platform allows (NETCI_ALLOWED_AGENT_LABELS)",
        )
    return label


def application_directory(value: Any) -> str:
    """The directory to build, inside the module's repository at the recorded commit.

    It chooses *what* is built from the module's own source, never where it goes, which
    is why a caller may set it at all. Relative and inside the checkout: a `..` segment or
    a leading `/` would point the build at the agent's filesystem instead -- including a
    previous build's workspace on a shared agent.
    """

    if not isinstance(value, str):
        raise BuildInputError("BUILD_INPUT_TYPE_NOT_ALLOWED", "NETCI_APP_DIR must be a string")
    path = value.strip().strip("/") if value.strip() not in {"", "."} else "."
    if path == ".":
        return "."
    if value.strip().startswith("/"):
        raise BuildInputError("BUILD_INPUT_INVALID", "NETCI_APP_DIR must be relative to the repository root")
    segments = path.split("/")
    if any(segment in {"", ".", ".."} for segment in segments) or not all(
        segment.replace("-", "").replace("_", "").replace(".", "").isalnum() for segment in segments
    ):
        raise BuildInputError(
            "BUILD_INPUT_INVALID",
            "NETCI_APP_DIR must be a plain relative path (letters, digits, '.', '_', '-' and "
            "'/'), with no '..' segment",
        )
    return path


def _approximate_size(value: Any) -> int:
    import json

    return len(json.dumps(value, default=str).encode("utf-8"))
