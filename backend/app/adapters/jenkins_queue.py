"""Builds submitted through netci-queue instead of to a Jenkins directly (ADR-082).

Jenkins keeps its queue in memory: a controller that dies loses every build waiting in it, and a
trigger whose answer was lost cannot tell whether the build exists. netci-queue accepts a run
durably (PostgreSQL) before it answers, dispatches it to the controllers once -- to a cell, or
across active-active replicas (ADR-081) -- and follows it to its result. The IDP's pipeline run id
is the idempotency key, so a trigger retried after a lost answer is the same run, never a second
build.

What still talks to Jenkins directly, through the same entry point (a VIP in front of replicas):
creating and updating the job (replicated between replicas, ADR-076) and reading a build's log.
Runs are read and cancelled through the queue, by run id: a Jenkins queue item id is one replica's,
and a build's stop would land on whichever replica the balancer chose.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .interfaces import JenkinsRun
from .jenkins_http import JenkinsHttpAdapter, JenkinsHttpConfig, JenkinsHttpError, _read_secret

if TYPE_CHECKING:
    from .ci_launcher import CiLaunchRequest

logger = logging.getLogger(__name__)

RUN_PREFIX = "queue:"

# netci-queue's state, and a finished run's Jenkins result, as the IDP's CI status. A refused run
# (the job or its parameters refused by the controller) failed; a result netci-queue could not
# learn (the build record gone) is not a status the IDP may act on.
_STATE = {"accepted": "queued", "dispatched": "queued", "started": "running", "cancelled": "cancelled", "refused": "failed"}
_RESULT = {"SUCCESS": "succeeded", "FAILURE": "failed", "UNSTABLE": "failed", "ABORTED": "cancelled", "NOT_BUILT": "failed"}


@dataclass(frozen=True)
class RunQueueConfig:
    url: str
    token: str
    timeout_seconds: float = 10.0
    # How long a submission is retried, with the same idempotency key, while the queue refuses
    # (its database failing over: ~30 s in the lab, ADR-080) before the launch fails.
    submit_retry_seconds: float = 120.0

    @classmethod
    def from_env(cls) -> "RunQueueConfig | None":
        url = _read_secret("NETCI_RUN_QUEUE_URL").rstrip("/")
        if not url:
            return None
        token = _read_secret("NETCI_RUN_QUEUE_TOKEN")
        if not token:
            raise ValueError("NETCI_RUN_QUEUE_URL is set but NETCI_RUN_QUEUE_TOKEN (or _FILE) is not")
        if not url.startswith(("http://", "https://")):
            raise ValueError("NETCI_RUN_QUEUE_URL must be http(s)")
        return cls(
            url=url,
            token=token,
            timeout_seconds=float(_read_secret("NETCI_RUN_QUEUE_TIMEOUT_SECONDS") or "10"),
            submit_retry_seconds=float(_read_secret("NETCI_RUN_QUEUE_SUBMIT_RETRY_SECONDS") or "120"),
        )


class RunQueueError(RuntimeError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


class JenkinsQueueAdapter(JenkinsHttpAdapter):
    """The Jenkins adapter whose runs go through netci-queue."""

    def __init__(self, config: JenkinsHttpConfig, queue: RunQueueConfig) -> None:
        super().__init__(config)
        self.queue = queue

    # ------------------------------------------------------------------ netci-queue

    def _queue_call(self, method: str, path: str, body: dict[str, Any] | None = None) -> tuple[int, dict[str, Any]]:
        req = urllib.request.Request(
            self.queue.url + path,
            method=method,
            data=None if body is None else json.dumps(body).encode(),
            headers={"Authorization": f"Bearer {self.queue.token}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.queue.timeout_seconds) as resp:
                return resp.status, json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as exc:
            try:
                payload = json.loads(exc.read() or b"{}")
            except ValueError:
                payload = {}
            return exc.code, payload if isinstance(payload, dict) else {}

    def _job_full_name(self, job_name: str) -> str:
        return f"{self.config.folder}/{job_name}" if self.config.folder else job_name

    @staticmethod
    def _queue_id(run_id: str) -> str | None:
        return run_id[len(RUN_PREFIX):] if run_id.startswith(RUN_PREFIX) else None

    def trigger_ci_run(self, job_name: str, request: "CiLaunchRequest", callback_token: str = "") -> JenkinsRun:
        """Submit the build; retried with the pipeline run's id as the key until the queue takes it.

        Accepted means durable: the run then exists whatever happens to this process or to any
        controller. A refusal that is not the queue being unavailable (a job no cell owns, a client
        that may not trigger it) is raised at once. The callback token goes as a parameter the
        queue keeps but never shows back (its secretParameters).
        """

        body = {
            "job": self._job_full_name(job_name),
            "parameters": self._ci_parameters(request, callback_token),
            "idempotencyKey": str(request.pipeline_run_id),
        }
        deadline = time.monotonic() + self.queue.submit_retry_seconds
        while True:
            try:
                status, run = self._queue_call("POST", "/v1/runs", body)
            except OSError as exc:  # connection refused, timeout: the queue not there right now
                status, run = 0, {"detail": f"{type(exc).__name__}: {exc}"}
            if status in (200, 201, 202):
                return JenkinsRun(run_id=RUN_PREFIX + str(run["id"]), status=self._status_of(run), console_url=self._console(run))
            if status not in (0, 502, 503, 504) or time.monotonic() >= deadline:
                raise RunQueueError(status, f"netci-queue did not accept the build (HTTP {status}): {run.get('detail', '')}"[:300])
            time.sleep(2)

    def _view(self, run_id: str) -> dict[str, Any]:
        status, run = self._queue_call("GET", f"/v1/runs/{urllib.parse.quote(run_id)}")
        if status != 200:
            raise RunQueueError(status, f"netci-queue answered HTTP {status} for run {run_id}")
        return run

    @staticmethod
    def _status_of(run: dict[str, Any]) -> str:
        state = str(run.get("state", ""))
        if state == "finished":
            return _RESULT.get(str(run.get("result", "")).upper(), "unknown")
        return _STATE.get(state, "unknown")

    def _console(self, run: dict[str, Any]) -> str | None:
        url = str(run.get("buildUrl") or "")
        return f"{self.config.base_url}/{url.lstrip('/')}" if url else None

    def get_status(self, run_id: str) -> JenkinsRun:
        qid = self._queue_id(run_id)
        if qid is None:
            return super().get_status(run_id)  # a run triggered before the queue was used
        run = self._view(qid)
        return JenkinsRun(run_id=run_id, status=self._status_of(run), console_url=self._console(run))

    def get_logs(self, run_id: str) -> list[str]:
        qid = self._queue_id(run_id)
        if qid is None:
            return super().get_logs(run_id)
        run = self._view(qid)
        number = run.get("buildNumber")
        if not number:
            return []  # nothing has run, so there is no console yet
        job = str(run.get("job", ""))
        path = "".join(f"/job/{urllib.parse.quote(part, safe='')}" for part in job.split("/"))
        try:
            _, _, body = self._request("GET", f"{path}/{int(number)}/consoleText", use_crumb=False)
        except JenkinsHttpError as exc:
            if exc.status == 404:
                return []
            raise
        return body.decode(errors="replace").splitlines()

    def abort(self, run_id: str) -> None:
        qid = self._queue_id(run_id)
        if qid is None:
            super().abort(run_id)
            return
        status, run = self._queue_call("POST", f"/v1/runs/{urllib.parse.quote(qid)}/cancel")
        if status in (200, 202):
            return
        raise RunQueueError(status, f"netci-queue did not cancel run {qid} (HTTP {status}): {run.get('detail', '')}"[:300])
