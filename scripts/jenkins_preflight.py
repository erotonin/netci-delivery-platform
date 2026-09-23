#!/usr/bin/env python3
"""Check an existing Jenkins controller before pointing netCI at it.

"Change the Jenkins endpoint and it works" is true only if the controller has what
netCI's jobs ask for. This asks the controller itself, with the service account netCI
will use, and says which of those things is missing -- before an install, instead of as
a failed first build.

    python3 scripts/jenkins_preflight.py --url https://jenkins.example.com --user netci-sa
    # the API token is read from JENKINS_API_TOKEN or --token-file, never an argument

Checks, each reported PASS/FAIL with what to do:
  reachable        the controller answers and the token authenticates
  crumb            CSRF crumbs can be issued (netCI's POSTs need one)
  plugins          the plugins the generated pipeline job uses are installed
  permissions      the account can create and configure jobs (netCI manages its own)
  library          the shared library the jobs load is configured, at the version asked
  cosign credential  the signing credential the Sign stage binds exists
  agents           something can run a build with the agent label

It never creates, changes or triggers anything on the controller. Exit status 0 means
every check passed.
"""

from __future__ import annotations

import argparse
import base64
import http.cookiejar
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

#: Plugins the job netCI generates depends on (see backend/app/adapters/jenkins_http.py
#: _job_config_xml and jenkins/shared-library): the pipeline job type, the declarative
#: syntax, shared libraries, credential binding for the cosign key, timestamps, and the
#: Kubernetes plugin when builds run in ephemeral pods.
REQUIRED_PLUGINS = {
    "workflow-job": "Pipeline job type",
    "workflow-cps": "Pipeline Groovy",
    "pipeline-model-definition": "Declarative pipeline",
    "pipeline-groovy-lib": "Shared libraries (older controllers: workflow-cps-global-lib)",
    "credentials-binding": "withCredentials, used to bind the cosign key",
    "plain-credentials": "Secret text credentials",
    "timestamper": "timestamps() in the job options",
    "git": "Checkout of the application repository",
}
KUBERNETES_PLUGIN = "kubernetes"
LIBRARY_PLUGIN_ALIASES = {"pipeline-groovy-lib", "workflow-cps-global-lib"}


class Controller:
    def __init__(self, url: str, user: str, token: str, timeout: float) -> None:
        self.url = url.rstrip("/")
        self.timeout = timeout
        self.auth = "Basic " + base64.b64encode(f"{user}:{token}".encode()).decode()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
        )

    def get(self, path: str) -> tuple[int, dict[str, str], bytes]:
        request = urllib.request.Request(self.url + path, headers={"Authorization": self.auth})
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                return response.status, dict(response.headers), response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers or {}), exc.read()

    def json(self, path: str) -> tuple[int, object]:
        status, _, body = self.get(path)
        try:
            return status, json.loads(body or b"{}")
        except json.JSONDecodeError:
            return status, None


class Report:
    def __init__(self) -> None:
        self.failed = 0

    def result(self, name: str, ok: bool, detail: str, fix: str = "") -> None:
        mark = "PASS" if ok else "FAIL"
        print(f"{mark}  {name:18} {detail}")
        if not ok:
            self.failed += 1
            if fix:
                print(f"      -> {fix}")


def check(controller: Controller, *, library: str, cosign_id: str, agent_label: str, report: Report) -> None:
    # reachable + authenticated
    try:
        status, who = controller.json("/me/api/json")
    except (urllib.error.URLError, OSError) as exc:
        report.result("reachable", False, f"{controller.url}: {exc}",
                      "check the URL and that netCI's network can reach the controller")
        return
    if status in (401, 403) or not isinstance(who, dict) or who.get("id") in (None, "anonymous"):
        report.result("reachable", False, f"HTTP {status}: the token did not authenticate",
                      "create an API token for the service account (User > Security > API Token)")
        return
    report.result("reachable", True, f"authenticated as {who.get('id')}")

    status, crumb = controller.json("/crumbIssuer/api/json")
    report.result("crumb", status == 200 and isinstance(crumb, dict) and "crumb" in crumb,
                  f"HTTP {status}", "enable CSRF protection's crumb issuer (default on)")

    # plugins
    status, plugins = controller.json("/pluginManager/api/json?depth=1&tree=plugins%5BshortName,version,active%5D")
    installed = {
        p["shortName"]: p for p in (plugins or {}).get("plugins", []) if isinstance(p, dict) and p.get("active")
    } if status == 200 else {}
    if not installed:
        report.result("plugins", False, f"HTTP {status}: cannot list plugins",
                      "the account needs Overall/Read, or an admin must confirm the list below by hand")
    else:
        missing = [
            f"{name} ({why})" for name, why in REQUIRED_PLUGINS.items()
            if name not in installed and not (name in LIBRARY_PLUGIN_ALIASES and LIBRARY_PLUGIN_ALIASES & set(installed))
        ]
        report.result("plugins", not missing,
                      "all present" if not missing else "missing: " + ", ".join(missing),
                      "install them from Manage Jenkins > Plugins")
        report.result("kubernetes plugin", KUBERNETES_PLUGIN in installed,
                      "present" if KUBERNETES_PLUGIN in installed else "absent",
                      "netciPipeline runs builds in ephemeral Kubernetes pods; install the kubernetes plugin "
                      "and a cloud with a pod template labelled for netCI")

    # permissions: what netCI does with the account is create and reconfigure its own jobs
    status_items, _ = controller.json("/api/json?tree=jobs%5Bname%5D")
    can_list = status_items == 200
    # Jenkins exposes the effective permission for "create item" as the newJob page.
    create_status, _, _ = controller.get("/view/all/newJob")
    report.result("permissions", can_list and create_status == 200,
                  f"list jobs: HTTP {status_items}, create job page: HTTP {create_status}",
                  "grant the account Overall/Read and Job/Create, Configure, Build, Read, Cancel")

    # shared library
    name, _, version = library.partition("@")
    status, _, body = controller.get("/manage/configure")
    page = body.decode(errors="replace") if status == 200 else ""
    if status != 200:
        report.result("library", False, f"HTTP {status}: cannot read the global configuration",
                      f"confirm by hand that Global Pipeline Library '{name}' exists"
                      + (f" and allows version '{version}'" if version else ""))
    else:
        found = f'value="{name}"' in page or f">{name}<" in page
        report.result("library", found,
                      f"'{name}' {'configured' if found else 'not found'}"
                      + (f"; version '{version}' is resolved by Jenkins at build time, not checked here" if version else ""),
                      f"add Global Pipeline Library '{name}' pointing at the repository that holds "
                      "jenkins/shared-library (Library Path: jenkins/shared-library/ when it is the netCI repository)")

    # cosign credential: existence only; its secret is never read
    status, creds = controller.json("/credentials/store/system/domain/_/api/json?depth=1&tree=credentials%5Bid%5D")
    ids = {c.get("id") for c in (creds or {}).get("credentials", []) if isinstance(c, dict)} if status == 200 else set()
    if status != 200:
        report.result("cosign credential", False, f"HTTP {status}: cannot list global credentials",
                      f"confirm by hand that a Secret text credential '{cosign_id}' exists")
    else:
        report.result("cosign credential", cosign_id in ids,
                      f"'{cosign_id}' {'exists' if cosign_id in ids else 'missing'}",
                      f"add a global Secret text credential '{cosign_id}' holding the cosign private key")

    # agents: a label with at least one executor, or a cloud that can provide one
    status, label = controller.json(f"/label/{urllib.parse.quote(agent_label)}/api/json?tree=nodes%5BnodeName%5D,clouds%5Bname%5D,totalExecutors")
    if status == 200 and isinstance(label, dict):
        clouds = [c.get("name") or c.get("_class", "cloud").rsplit(".", 1)[-1]
                  for c in label.get("clouds", []) if isinstance(c, dict)]
        nodes = label.get("nodes") or []
        ok = bool(clouds) or bool(nodes)
        report.result("agents", ok,
                      f"label '{agent_label}': {len(nodes)} static node(s), clouds {clouds or 'none'}",
                      f"add a Kubernetes pod template (or a node) labelled '{agent_label}'")
    else:
        report.result("agents", False, f"label '{agent_label}': HTTP {status}",
                      f"add a Kubernetes pod template (or a node) labelled '{agent_label}'")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", required=True)
    parser.add_argument("--user", required=True)
    parser.add_argument("--token-file", help="file holding the API token (default: $JENKINS_API_TOKEN)")
    parser.add_argument("--library", default="netci-shared-library", help="name[@version] the jobs load")
    parser.add_argument("--cosign-credential", default="netci-cosign-key")
    parser.add_argument("--agent-label", default="netci-ephemeral")
    parser.add_argument("--timeout", type=float, default=15.0)
    args = parser.parse_args()

    token = (open(args.token_file, encoding="utf-8").read().strip() if args.token_file
             else os.getenv("JENKINS_API_TOKEN", "").strip())
    if not token:
        print("an API token is required: --token-file or JENKINS_API_TOKEN", file=sys.stderr)
        return 2

    report = Report()
    print(f"netCI preflight for {args.url} as {args.user}")
    check(Controller(args.url, args.user, token, args.timeout), library=args.library,
          cosign_id=args.cosign_credential, agent_label=args.agent_label, report=report)
    print("ready for netCI" if not report.failed else f"{report.failed} check(s) failed")
    return 0 if not report.failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
