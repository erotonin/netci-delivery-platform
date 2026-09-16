"""Seam between the delivery domain and whichever engine actually runs CI.

`DeliveryPlatform` only knows `CiLauncher.launch(...) -> LaunchedCi | None`.  That
keeps Jenkins credentials, job XML and queue polling out of the domain while still
letting the local reference implementation close the loop against real controllers.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field, replace
from typing import Protocol
from uuid import UUID

from .interfaces import JenkinsAdapter
from .jenkins_router import ControllerState, JenkinsController, JenkinsRouter
from .. import workload_identity
from .build_isolation import (
    BuildIsolation,
    BuildIsolationError,
    BuildIsolationProvisioner,
    SharedNamespaceIsolation,
    build_isolation_provisioner,
)
from ..runtime_environment import require_live_mode

logger = logging.getLogger(__name__)


class CiLaunchError(RuntimeError):
    """Raised when no CI engine could accept the run."""


@dataclass(frozen=True)
class CiLaunchRequest:
    application_id: UUID
    application_name: str
    repository_url: str
    pipeline_template: str
    runtime: str
    stages: tuple[str, ...]
    pipeline_run_id: UUID
    commit_sha: str
    branch: str
    environment: str
    correlation_id: str
    parameters: dict[str, object]
    # Where the build pod runs and which cache it mounts. Set by the launcher from the
    # isolation provisioner; None only when isolation is explicitly `none`.
    isolation: BuildIsolation | None = None
    # Custom catalog stages in this run's list: id, name, repository script, anchor.
    custom_stages: list[dict[str, object]] = field(default_factory=list)


@dataclass(frozen=True)
class LaunchedCi:
    """What the domain records about an external CI run."""

    controller_id: str
    external_run_id: str
    console_url: str | None = None

    @property
    def jenkins_run_id(self) -> str:
        """Stable, controller-qualified identity stored on the pipeline run."""

        return f"{self.controller_id}:{self.external_run_id}"


class CiLauncher(Protocol):
    def launch(self, request: CiLaunchRequest) -> LaunchedCi | None: ...
    def abort(self, jenkins_run_id: str) -> None: ...
    def get_status(self, jenkins_run_id: str) -> str | None: ...


class NullCiLauncher:
    """Default launcher: the API records the run and waits for an external callback.

    This is the honest local-reference behaviour — it never pretends a build started.
    """

    mode = "none"

    def launch(self, request: CiLaunchRequest) -> LaunchedCi | None:
        return None

    def abort(self, jenkins_run_id: str) -> None:
        return None

    def get_status(self, jenkins_run_id: str) -> str | None:
        return None


class JenkinsCiLauncher:
    """Route a queued run to a healthy controller and trigger the real job."""

    mode = "jenkins"

    def __init__(
        self,
        router: JenkinsRouter,
        adapters: dict[str, JenkinsAdapter],
        *,
        required_capability: str | None = None,
        isolation: BuildIsolationProvisioner | None = None,
    ) -> None:
        self.router = router
        self.adapters = adapters
        self.required_capability = required_capability
        self.isolation = isolation or SharedNamespaceIsolation()

    def controller_drift(self) -> dict[str, object]:
        """Compare what every controller is running (ADR-030).

        Same JCasC from git on every controller is the intent; this reports whether it
        is the fact. Jobs are compared as a set minus netCI's own jobs, which the router
        creates on whichever controller first takes a build for the application.
        """

        fingerprints: dict[str, dict[str, object]] = {}
        errors: dict[str, str] = {}
        for controller in self.router.controllers:
            adapter = self.adapters.get(controller.controller_id)
            probe = getattr(adapter, "configuration_fingerprint", None)
            if probe is None:
                continue
            try:
                fingerprints[controller.controller_id] = probe()
            except Exception as exc:  # noqa: BLE001 - reported, not raised
                errors[controller.controller_id] = f"{type(exc).__name__}: {exc}"
        differing: list[str] = []
        for key in ("jcascNormalizedSha256", "pluginsSha256"):
            if len({str(fp.get(key)) for fp in fingerprints.values()}) > 1:
                differing.append(key)
        job_sets = {
            cid: {job for job in fp.get("jobs", []) if not str(job).startswith("netci-")}
            for cid, fp in fingerprints.items()
        }
        if len({frozenset(v) for v in job_sets.values()}) > 1:
            differing.append("jobs")
        return {
            "controllers": fingerprints,
            "unreachable": errors,
            "drift": bool(differing) or bool(errors),
            "differing": differing,
        }

    def reload_controllers(self) -> dict[str, object]:
        """Reload JCasC on every controller, then compare them (ADR-033).

        The reload is what a push to the configuration repository (or a secret
        rotation) triggers; the comparison afterwards is what says whether the
        controllers still agree. A controller that refused the reload is reported,
        not hidden -- it is now the one running the old configuration.
        """

        reloaded: dict[str, dict[str, object]] = {}
        for controller in self.router.controllers:
            adapter = self.adapters.get(controller.controller_id)
            reload = getattr(adapter, "reload_configuration", None)
            if reload is None:
                continue
            try:
                reload()
                reloaded[controller.controller_id] = {"reloaded": True}
            except Exception as exc:  # noqa: BLE001 - reported per controller
                reloaded[controller.controller_id] = {"reloaded": False, "error": f"{type(exc).__name__}: {exc}"[:300]}
        drift = self.controller_drift()
        return {"controllers": reloaded, "drift": drift, "ok": all(r["reloaded"] for r in reloaded.values()) and not drift["drift"]}

    def refresh_health(self) -> None:
        """Ask every adapter whether its controller answers, before routing."""

        for controller in self.router.controllers:
            adapter = self.adapters.get(controller.controller_id)
            probe = getattr(adapter, "health_check", None)
            if probe is None:
                continue
            controller.state = ControllerState.HEALTHY if probe() else ControllerState.UNAVAILABLE

    def launch(self, request: CiLaunchRequest) -> LaunchedCi:
        self.refresh_health()
        capability = self.required_capability or request.runtime
        # The project's namespace, service account and cache must exist before a pod is
        # asked for. Idempotent, so it also repairs a namespace someone deleted; fails
        # closed, so a build never lands in the shared namespace by accident.
        try:
            isolation = self.isolation.ensure(request.application_id, request.application_name)
        except BuildIsolationError as exc:
            raise CiLaunchError(f"build isolation unavailable: {exc}") from exc
        request = replace(request, isolation=isolation)
        attempted: list[str] = []
        last_error: Exception | None = None
        # One token per build, minted here and handed to Jenkins as a masked parameter.
        # It can report for this run and nothing else, which is what replaces the shared
        # key every controller used to hold. Skipped only when no signing key is
        # configured -- allowed in local mode, refused at startup everywhere else.
        callback_token = ""
        if workload_identity.workload_identity_configured():
            callback_token = workload_identity.mint(
                workload=workload_identity.Workload.JENKINS,
                application_id=request.application_id,
                pipeline_run_id=request.pipeline_run_id,
                scopes=set(workload_identity.WORKLOAD_SCOPES[workload_identity.Workload.JENKINS]),
                # A queued build can wait on a busy controller; the token must outlast it.
                ttl_seconds=workload_identity.MAX_TTL_SECONDS,
            )
        # Routing is a preference, not a guarantee: a controller can die between the
        # health probe and the trigger, so fall through to the next candidate.
        while True:
            try:
                controller = self.router.choose_for_new_build(capability)
            except RuntimeError as exc:
                message = "no Jenkins controller accepted the build"
                if attempted:
                    message = f"{message} (tried {', '.join(attempted)})"
                raise CiLaunchError(message) from (last_error or exc)
            adapter = self.adapters.get(controller.controller_id)
            if adapter is None:
                self.router.mark_unavailable(controller.controller_id)
                continue
            attempted.append(controller.controller_id)
            try:
                job_name = adapter.create_or_update_job(request.application_id, request.pipeline_template)
                run = adapter.trigger_ci_run(job_name, request, callback_token)
            except Exception as exc:  # adapter transport failure -> try the next controller
                last_error = exc
                logger.warning("controller %s rejected the build: %s", controller.controller_id, exc)
                self.router.mark_unavailable(controller.controller_id)
                continue
            controller.queue_depth += 1
            return LaunchedCi(
                controller_id=controller.controller_id,
                external_run_id=run.run_id,
                console_url=run.console_url,
            )

    def abort(self, jenkins_run_id: str) -> None:
        controller_id, _, external_run_id = jenkins_run_id.partition(":")
        adapter = self.adapters.get(controller_id)
        if adapter is not None and external_run_id:
            try:
                adapter.abort(external_run_id)
            except Exception as exc:
                logger.warning("failed to abort Jenkins run %s on %s: %s", external_run_id, controller_id, exc)

    def get_status(self, jenkins_run_id: str) -> str | None:
        controller_id, _, external_run_id = jenkins_run_id.partition(":")
        adapter = self.adapters.get(controller_id)
        if adapter is not None and external_run_id:
            try:
                run = adapter.get_status(external_run_id)
                return run.status
            except Exception as exc:
                logger.debug("failed to get status for Jenkins run %s on %s: %s", external_run_id, controller_id, exc)
                return None
        return None


def build_ci_launcher() -> CiLauncher:
    """Compose the configured launcher from the environment (see docs/api-contract.md)."""

    mode = os.getenv("NETCI_CI_MODE", "none").strip().lower()
    require_live_mode("NETCI_CI_MODE", mode)
    if mode in {"", "none", "callback"}:
        return NullCiLauncher()
    if mode != "jenkins":
        raise ValueError(f"unsupported NETCI_CI_MODE: {mode}")

    from .jenkins_http import JenkinsHttpAdapter, JenkinsHttpConfig

    controller_ids = [item.strip() for item in os.getenv("NETCI_JENKINS_CONTROLLERS", "A,B").split(",") if item.strip()]
    controllers: list[JenkinsController] = []
    adapters: dict[str, JenkinsAdapter] = {}
    for controller_id in controller_ids:
        prefix = f"JENKINS_{controller_id.upper().replace('-', '_')}"
        config = JenkinsHttpConfig.from_env(prefix)
        name = os.getenv(f"{prefix}_ID", f"jenkins-{controller_id.lower()}")
        executors = int(os.getenv(f"{prefix}_EXECUTORS", "2"))
        controllers.append(JenkinsController(controller_id=name, executors_total=executors))
        adapters[name] = JenkinsHttpAdapter(config)
    if not controllers:
        raise ValueError("NETCI_CI_MODE=jenkins requires at least one controller in NETCI_JENKINS_CONTROLLERS")
    return JenkinsCiLauncher(JenkinsRouter(controllers), adapters, isolation=build_isolation_provisioner())
