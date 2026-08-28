"""Seam between the delivery domain and whichever engine actually runs CI.

`DeliveryPlatform` only knows `CiLauncher.launch(...) -> LaunchedCi | None`.  That
keeps Jenkins credentials, job XML and queue polling out of the domain while still
letting the local reference implementation close the loop against real controllers.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from .interfaces import JenkinsAdapter
from .jenkins_router import ControllerState, JenkinsController, JenkinsRouter

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


class NullCiLauncher:
    """Default launcher: the API records the run and waits for an external callback.

    This is the honest local-reference behaviour — it never pretends a build started.
    """

    mode = "none"

    def launch(self, request: CiLaunchRequest) -> LaunchedCi | None:
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
    ) -> None:
        self.router = router
        self.adapters = adapters
        self.required_capability = required_capability

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
        attempted: list[str] = []
        last_error: Exception | None = None
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
                run = adapter.trigger_ci_run(job_name, request)
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


def build_ci_launcher() -> CiLauncher:
    """Compose the configured launcher from the environment (see docs/api-contract.md)."""

    mode = os.getenv("NETCI_CI_MODE", "none").strip().lower()
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
    return JenkinsCiLauncher(JenkinsRouter(controllers), adapters)
