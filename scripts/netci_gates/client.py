"""Minimal netCI API client for the gate runners.

Gates drive the platform the same way the Portal and Jenkins do -- over HTTP, with
the same idempotency, correlation and pipeline-key headers -- so a gate proves the
real contract rather than an internal shortcut.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
import uuid
from typing import Any


class ApiError(RuntimeError):
    def __init__(self, status: int, payload: Any, path: str) -> None:
        super().__init__(f"{path} -> {status}: {payload}")
        self.status = status
        self.payload = payload
        self.path = path


class NetciClient:
    def __init__(self, base_url: str, pipeline_api_key: str = "", *, timeout: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.pipeline_api_key = pipeline_api_key
        self.timeout = timeout

    def request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        machine: bool = False,
        idempotency_key: str | None = None,
        correlation_id: str | None = None,
    ) -> tuple[int, Any]:
        headers = {"Accept": "application/json", "X-Correlation-Id": correlation_id or str(uuid.uuid4())}
        body = None
        if payload is not None:
            body = json.dumps(payload).encode()
            headers["Content-Type"] = "application/json"
        if machine:
            headers["Authorization"] = f"Bearer {self.pipeline_api_key}"
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        request = urllib.request.Request(f"{self.base_url}{path}", data=body, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode(errors="replace")
                return response.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode(errors="replace")
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                parsed = raw
            return exc.code, parsed
        except urllib.error.URLError as exc:
            raise ApiError(0, str(exc.reason), path) from exc

    def expect(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        status: int | tuple[int, ...] = 200,
        **kwargs: Any,
    ) -> Any:
        wanted = status if isinstance(status, tuple) else (status,)
        code, parsed = self.request(method, path, payload, **kwargs)
        if code not in wanted:
            raise ApiError(code, parsed, path)
        return parsed

    # ------------------------------------------------------------- convenience

    def health(self) -> dict[str, Any]:
        return self.expect("GET", "/healthz")

    def create_application(self, payload: dict[str, Any], idempotency_key: str) -> dict[str, Any]:
        return self.expect("POST", "/applications", payload, status=201, idempotency_key=idempotency_key)

    def start_pipeline(self, application_id: str, payload: dict[str, Any], idempotency_key: str) -> dict[str, Any]:
        return self.expect(
            "POST",
            f"/applications/{application_id}/pipeline-runs",
            payload,
            status=202,
            idempotency_key=idempotency_key,
        )

    def ci_result(self, run_id: str, payload: dict[str, Any], *, status: int | tuple[int, ...] = 202) -> Any:
        return self.expect("POST", f"/pipeline-runs/{run_id}/ci-result", payload, status=status, machine=True)

    def publish_evidence(self, run_id: str, payload: dict[str, Any], *, status: int | tuple[int, ...] = 202) -> Any:
        return self.expect(
            "POST", f"/pipeline-runs/{run_id}/security-evidence", payload, status=status, machine=True
        )

    def approve(self, deployment_id: str) -> dict[str, Any]:
        """Approve as whoever this client's credential belongs to.

        No actor in the body: netCI records the authenticated principal and ignores any
        actor a caller supplies, so sending one would only look like it worked.
        """

        return self.expect("POST", f"/deployments/{deployment_id}/approve", {}, status=202)

    def whoami(self) -> dict[str, Any]:
        """The identity netCI will attribute this client's actions to."""

        return self.expect("GET", "/me")

    def deployment_result(self, deployment_id: str, status_value: str, message: str) -> dict[str, Any]:
        return self.expect(
            "POST",
            f"/deployments/{deployment_id}/result",
            {"status": status_value, "message": message},
            status=202,
            machine=True,
        )

    def rollback(self, deployment_id: str, digest: str, reason: str) -> dict[str, Any]:
        return self.expect(
            "POST",
            f"/deployments/{deployment_id}/rollback",
            {"targetArtifactDigest": digest, "reason": reason},
            status=202,
        )

    def pipeline(self, run_id: str) -> dict[str, Any]:
        return self.expect("GET", f"/pipeline-runs/{run_id}")

    def pipeline_logs(self, run_id: str) -> dict[str, Any]:
        return self.expect("GET", f"/pipeline-runs/{run_id}/logs")

    def dora(self, application_id: str) -> dict[str, Any]:
        return self.expect("GET", f"/applications/{application_id}/dora")

    def delivery_events(self, application_id: str) -> dict[str, Any]:
        return self.expect("GET", f"/delivery-events?applicationId={application_id}")
