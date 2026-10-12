"""A small Jenkins REST client for the gates.

Two Jenkins details are easy to get wrong and are handled once here:

* CSRF. A POST needs a crumb *and* the session the crumb was issued for, so the client
  keeps a cookie jar. Fetching a crumb with plain basic auth and no cookie gives a
  crumb Jenkins will reject.
* Where configuration actually lives. The root `/api/json` does not expose clouds, so
  asking it whether the Kubernetes cloud exists silently answers "no". The label
  endpoint is the honest place to look.
"""

from __future__ import annotations

import base64
import hashlib
import http.cookiejar
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


class JenkinsError(RuntimeError):
    pass


class JenkinsClient:
    def __init__(self, base_url: str, username: str, token: str, *, timeout: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._auth = base64.b64encode(f"{username}:{token}".encode()).decode()
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
        )
        self._crumb: tuple[str, str] | None = None

    # ---------------------------------------------------------------- transport

    def _open(self, request: urllib.request.Request, timeout: float | None = None) -> bytes:
        request.add_header("Authorization", f"Basic {self._auth}")
        with self._opener.open(request, timeout=timeout or self.timeout) as response:
            return response.read()

    def get_bytes(self, path: str, *, timeout: float | None = None) -> bytes:
        return self._open(urllib.request.Request(f"{self.base_url}{path}"), timeout)

    def get_text(self, path: str, *, timeout: float | None = None) -> str:
        return self.get_bytes(path, timeout=timeout).decode(errors="replace")

    def get_json(self, path: str, *, timeout: float | None = None) -> dict[str, Any]:
        body = self.get_bytes(path, timeout=timeout)
        return json.loads(body or b"{}")

    def crumb(self) -> tuple[str, str] | None:
        if self._crumb is None:
            try:
                payload = self.get_json("/crumbIssuer/api/json", timeout=10)
                self._crumb = (str(payload["crumbRequestField"]), str(payload["crumb"]))
            except (urllib.error.URLError, OSError, KeyError, ValueError):
                return None
        return self._crumb

    def _reset_session(self) -> None:
        self._crumb = None
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
        )

    def post(
        self,
        path: str,
        *,
        body: bytes | None = None,
        content_type: str | None = None,
        timeout: float | None = None,
        _retried: bool = False,
    ) -> tuple[int, dict[str, str], bytes]:
        body_bytes = body
        request = urllib.request.Request(f"{self.base_url}{path}", data=body or b"", method="POST")
        if content_type:
            request.add_header("Content-Type", content_type)
        crumb = self.crumb()
        if crumb:
            request.add_header(*crumb)
        request.add_header("Authorization", f"Basic {self._auth}")
        try:
            with self._opener.open(request, timeout=timeout or self.timeout) as response:
                return response.status, dict(response.headers.items()), response.read()
        except urllib.error.HTTPError as exc:
            body = exc.read()
            # A crumb belongs to the session it was issued in. After the controller
            # restarts -- which the rebuild gate does on purpose -- the cached crumb and
            # cookie are both dead, and every POST fails until they are re-fetched.
            if exc.code == 403 and not _retried and b"crumb" in body.lower():
                self._reset_session()
                return self.post(
                    path, body=body_bytes, content_type=content_type, timeout=timeout, _retried=True
                )
            return exc.code, dict(exc.headers.items()), body

    # ------------------------------------------------------------------ queries

    def reachable(self, timeout: float = 5.0) -> bool:
        try:
            self.get_json("/api/json?tree=mode", timeout=timeout)
            return True
        except (urllib.error.URLError, OSError, json.JSONDecodeError):
            return False

    def clouds_for_label(self, label: str) -> list[str]:
        """Which cloud implementations can provision this label.

        This is what proves the ephemeral agent cloud is live: a label nothing can
        provision has an empty list, however good the YAML looks.
        """

        try:
            payload = self.get_json(f"/label/{urllib.parse.quote(label)}/api/json")
        except (urllib.error.URLError, OSError, json.JSONDecodeError):
            return []
        return [str(item.get("_class", "")) for item in payload.get("clouds", [])]

    def online_labels(self) -> set[str]:
        payload = self.get_json(
            "/computer/api/json?tree=computer%5BdisplayName,offline,assignedLabels%5Bname%5D%5D"
        )
        return {
            label["name"]
            for computer in payload.get("computer", [])
            if not computer.get("offline")
            for label in computer.get("assignedLabels", [])
        }

    def credential_ids(self, store: str = "system") -> list[str]:
        try:
            payload = self.get_json(
                f"/manage/credentials/store/{store}/domain/_/api/json?tree=credentials%5Bid%5D"
            )
        except (urllib.error.URLError, OSError, json.JSONDecodeError):
            return []
        return sorted(str(item["id"]) for item in payload.get("credentials", []) if item.get("id"))

    def export_configuration(self) -> str:
        """The live JCasC document, which is the comparable form of "the configuration"."""

        status, _, body = self.post("/manage/configuration-as-code/export", timeout=60)
        if not 200 <= status < 300:
            raise JenkinsError(f"JCasC export returned {status}: {body[:300]!r}")
        return body.decode(errors="replace")

    @staticmethod
    def normalize_configuration(configuration: str) -> str:
        """Remove per-instance values so two exports can be compared for equality.

        Two kinds of value differ between instances even when the configuration is
        identical, and comparing raw bytes would fail on both while telling you nothing:

        * encrypted secrets -- Jenkins encrypts with a key generated per JENKINS_HOME,
          so the same plaintext produces different ciphertext of the same length;
        * generated identifiers -- a pod template gets a fresh UUID on each startup.

        Credential ids, cloud names and job names are compared separately, so a piece
        of configuration going missing is still caught.
        """

        configuration = re.sub(r"\{AQAAAB[A-Za-z0-9+/=]+\}", "{REDACTED}", configuration)
        return re.sub(
            r'id: "[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"',
            'id: "{GENERATED}"',
            configuration,
        )

    def fingerprint(self) -> dict[str, Any]:
        root = self.get_json("/api/json?tree=mode,numExecutors,quietingDown")
        plugins = self.get_json("/pluginManager/api/json?depth=1&tree=plugins%5BshortName,version%5D")
        jobs = self.get_json("/api/json?tree=jobs%5Bname%5D")
        exported = self.export_configuration()
        return {
            "mode": root.get("mode"),
            "numExecutors": root.get("numExecutors"),
            "plugins": sorted(
                [item["shortName"], item["version"]] for item in plugins.get("plugins", [])
            ),
            "jcascExportSha256": hashlib.sha256(exported.encode()).hexdigest(),
            "jcascNormalizedSha256": hashlib.sha256(self.normalize_configuration(exported).encode()).hexdigest(),
            "jcascExportBytes": len(exported),
            "jobs": sorted(str(item["name"]) for item in jobs.get("jobs", [])),
            "credentialIds": self.credential_ids(),
            "ephemeralLabelClouds": self.clouds_for_label("netci-ephemeral"),
        }

    def build(self, job: str, number: int) -> dict[str, Any]:
        return self.get_json(f"/job/{urllib.parse.quote(job)}/{number}/api/json")

    def console(self, job: str, number: int) -> str:
        return self.get_text(f"/job/{urllib.parse.quote(job)}/{number}/consoleText", timeout=60)


def resolve_remote_commit(git_url: str, branch: str = "main") -> str:
    """The commit a build will actually check out.

    A branch name is not a commit, and netCI refuses one: a pipeline run records the
    exact revision it built, so the gate has to resolve the ref before starting a run.
    """

    import subprocess

    result = subprocess.run(
        ["git", "ls-remote", git_url, branch, f"refs/heads/{branch}"],
        capture_output=True, text=True, check=False, timeout=60,
    )
    for line in result.stdout.splitlines():
        sha = line.split("\t", 1)[0].strip()
        if len(sha) == 40:
            return sha
    raise JenkinsError(f"could not resolve {branch} at {git_url}: {result.stderr.strip() or result.stdout.strip()}")
