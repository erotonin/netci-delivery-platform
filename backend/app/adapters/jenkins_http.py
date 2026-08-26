from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from .interfaces import JenkinsRun


class JenkinsHttpError(RuntimeError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass(frozen=True)
class JenkinsHttpConfig:
    base_url: str
    username: str
    api_token: str
    timeout_seconds: float = 10.0

    @classmethod
    def from_env(cls, prefix: str = "JENKINS") -> "JenkinsHttpConfig":
        base_url = os.getenv(f"{prefix}_URL", "").strip().rstrip("/")
        username = os.getenv(f"{prefix}_USERNAME", "").strip()
        api_token = os.getenv(f"{prefix}_API_TOKEN", "").strip()
        if not base_url or not username or not api_token:
            raise ValueError(f"{prefix}_URL, {prefix}_USERNAME and {prefix}_API_TOKEN are required")
        return cls(base_url, username, api_token)


class JenkinsHttpAdapter:
    """Small Jenkins REST adapter with no Jenkins details leaking into domain code."""

    def __init__(self, config: JenkinsHttpConfig) -> None:
        self.config = config

    def _url(self, path: str) -> str:
        return f"{self.config.base_url}/{path.lstrip('/')}"

    def _request(self, method: str, path: str, *, body: bytes | None = None, content_type: str = "application/json") -> tuple[int, dict[str, str], bytes]:
        request = urllib.request.Request(self._url(path), data=body, method=method)
        credentials = f"{self.config.username}:{self.config.api_token}".encode()
        import base64
        request.add_header("Authorization", f"Basic {base64.b64encode(credentials).decode()}")
        request.add_header("Accept", "application/json")
        if body is not None:
            request.add_header("Content-Type", content_type)
        try:
            with urllib.request.urlopen(request, timeout=self.config.timeout_seconds) as response:
                return response.status, dict(response.headers.items()), response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[-1000:]
            raise JenkinsHttpError(exc.code, f"Jenkins {method} {path} failed: {detail}") from exc
        except urllib.error.URLError as exc:
            raise JenkinsHttpError(503, f"Jenkins unavailable: {exc.reason}") from exc

    def health_check(self) -> bool:
        try:
            status, _, _ = self._request("GET", "/api/json?tree=mode")
            return 200 <= status < 300
        except JenkinsHttpError:
            return False

    def create_or_update_job(self, application_id: UUID, template_id: str) -> str:
        job_name = f"netci-{application_id}"
        config_xml = f"""<?xml version=\"1.1\" encoding=\"UTF-8\"?>
<project>
  <description>Managed by netCI template {template_id}</description>
  <keepDependencies>false</keepDependencies>
  <scm class=\"hudson.plugins.git.GitSCM\" plugin=\"git\"/>
  <disabled>false</disabled>
</project>""".encode()
        try:
            self._request("POST", f"/createItem?name={urllib.parse.quote(job_name)}", body=config_xml, content_type="application/xml")
        except JenkinsHttpError as exc:
            if exc.status != 400:
                raise
            self._request("POST", f"/job/{urllib.parse.quote(job_name)}/config.xml", body=config_xml, content_type="application/xml")
        return job_name

    def trigger_ci(self, job_name: str, commit_sha: str, correlation_id: str) -> JenkinsRun:
        query = urllib.parse.urlencode({"COMMIT_SHA": commit_sha, "NETCI_CORRELATION_ID": correlation_id})
        _, headers, _ = self._request("POST", f"/job/{urllib.parse.quote(job_name)}/buildWithParameters?{query}")
        queue_location = next((value for key, value in headers.items() if key.lower() == "location"), None)
        return JenkinsRun(run_id=queue_location or f"{job_name}/queue", status="queued")

    def get_status(self, run_id: str) -> JenkinsRun:
        path = run_id if run_id.endswith("/api/json") else f"/job/{run_id}/api/json"
        _, _, body = self._request("GET", path)
        payload: dict[str, Any] = json.loads(body or b"{}")
        result = payload.get("result")
        building = bool(payload.get("building", False))
        status = "running" if building else (str(result).lower() if result else "queued")
        return JenkinsRun(run_id=run_id, status=status, console_url=payload.get("url"))

    def get_logs(self, run_id: str) -> list[str]:
        _, _, body = self._request("GET", f"/job/{run_id}/consoleText")
        return body.decode(errors="replace").splitlines()

    def abort(self, run_id: str) -> None:
        self._request("POST", f"/job/{run_id}/stop")
