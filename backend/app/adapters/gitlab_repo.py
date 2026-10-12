import json
import os
import urllib.parse
import urllib.request
import urllib.error
from typing import Any

from .scm_reporter import _read_secret

class GitLabRepoError(RuntimeError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class GitLabRepoClient:
    def __init__(self) -> None:
        self._gitlab_token = _read_secret("NETCI_GITLAB_TOKEN")
        self._gitlab_url = os.getenv("NETCI_GITLAB_URL", "https://gitlab.com").rstrip("/")
        self._timeout = 10.0

    def _headers(self) -> dict[str, str]:
        if not self._gitlab_token:
            raise GitLabRepoError(503, "NETCI_GITLAB_TOKEN_FILE is not configured")
        return {
            "PRIVATE-TOKEN": self._gitlab_token,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _request(self, method: str, url: str, data: dict[str, Any] | None = None) -> Any:
        req = urllib.request.Request(url, method=method, headers=self._headers())
        if data is not None:
            req.data = json.dumps(data).encode("utf-8")
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as response:
                body = response.read()
            # A proxy in front of GitLab can answer 200 with an HTML page: that is GitLab
            # failing, not netCI, and it must reach the caller as a GitLab error.
            return json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GitLabRepoError(502, "GitLab API returned a body that is not JSON") from exc
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")[:300]
            raise GitLabRepoError(exc.code, f"GitLab API returned {exc.code}: {body}") from exc
        except urllib.error.URLError as exc:
            raise GitLabRepoError(500, f"GitLab API connection error: {exc.reason}") from exc
        except TimeoutError as exc:
            raise GitLabRepoError(504, "GitLab API request timed out") from exc

    def get_file(self, project: str, path: str, ref: str) -> str | None:
        if not self._gitlab_token:
            raise GitLabRepoError(503, "NETCI_GITLAB_TOKEN_FILE is not configured")
        quoted_project = urllib.parse.quote(project, safe="")
        quoted_path = urllib.parse.quote(path, safe="")
        quoted_ref = urllib.parse.quote(ref, safe="")
        url = f"{self._gitlab_url}/api/v4/projects/{quoted_project}/repository/files/{quoted_path}/raw?ref={quoted_ref}"
        req = urllib.request.Request(url, headers=self._headers())
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as response:
                return response.read().decode("utf-8")
        except UnicodeDecodeError as exc:
            raise GitLabRepoError(502, f"{path} is not UTF-8 text") from exc
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            body = exc.read().decode("utf-8", errors="replace")[:300]
            raise GitLabRepoError(exc.code, f"GitLab API returned {exc.code}: {body}") from exc
        except urllib.error.URLError as exc:
            raise GitLabRepoError(500, f"GitLab API connection error: {exc.reason}") from exc
        except TimeoutError as exc:
            raise GitLabRepoError(504, "GitLab API request timed out") from exc

    def default_branch(self, project: str) -> str:
        quoted_project = urllib.parse.quote(project, safe="")
        url = f"{self._gitlab_url}/api/v4/projects/{quoted_project}"
        resp = self._request("GET", url)
        return str(resp.get("default_branch", "main"))

    def head_commit(self, project: str, branch: str) -> str:
        quoted_project = urllib.parse.quote(project, safe="")
        quoted_branch = urllib.parse.quote(branch, safe="")
        resp = self._request("GET", f"{self._gitlab_url}/api/v4/projects/{quoted_project}/repository/branches/{quoted_branch}")
        commit = (resp or {}).get("commit") or {}
        if not commit.get("id"):
            raise GitLabRepoError(502, f"GitLab returned no commit for branch {branch}")
        return str(commit["id"])

    def commit_files(self, project: str, branch: str, start_branch: str, message: str, actions: list[dict[str, str]]) -> dict[str, Any]:
        quoted_project = urllib.parse.quote(project, safe="")
        url = f"{self._gitlab_url}/api/v4/projects/{quoted_project}/repository/commits"
        body = {
            "branch": branch,
            "start_branch": start_branch,
            "commit_message": message,
            "actions": actions,
        }
        return self._request("POST", url, data=body)

    def open_merge_request(self, project: str, source_branch: str, target_branch: str, title: str, description: str) -> dict[str, Any]:
        quoted_project = urllib.parse.quote(project, safe="")
        url = f"{self._gitlab_url}/api/v4/projects/{quoted_project}/merge_requests"
        body = {
            "source_branch": source_branch,
            "target_branch": target_branch,
            "title": title,
            "description": description,
        }
        resp = self._request("POST", url, data=body)
        return {
            "iid": resp["iid"],
            "web_url": resp["web_url"],
        }

def get_file(project: str, path: str, ref: str) -> str | None:
    return GitLabRepoClient().get_file(project, path, ref)

def default_branch(project: str) -> str:
    return GitLabRepoClient().default_branch(project)

def head_commit(project: str, branch: str) -> str:
    return GitLabRepoClient().head_commit(project, branch)

def commit_files(project: str, branch: str, start_branch: str, message: str, actions: list[dict[str, str]]) -> dict[str, Any]:
    return GitLabRepoClient().commit_files(project, branch, start_branch, message, actions)

def open_merge_request(project: str, source_branch: str, target_branch: str, title: str, description: str) -> dict[str, Any]:
    return GitLabRepoClient().open_merge_request(project, source_branch, target_branch, title, description)
