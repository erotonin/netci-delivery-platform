#!/usr/bin/env python3
"""Trigger one Jenkins build and report its phase timings, for the benchmark.

`scripts/benchmark.py` runs a command per sample and reads a `NETCI_TIMING_JSON=` line
from its output. This is that command. Every number comes from Jenkins itself -- the
queue item, the build record and the stage graph -- rather than from the code under
test asserting how fast it was.

    python scripts/jenkins_build.py --job netci-bench --label netci-ephemeral

Phases:
  queue         queued until an executor picked it up. For an ephemeral agent this is
                where pod provisioning is paid, which is the point of the comparison.
  provisioning  queued until the agent pod reports Running (0 for a shared agent,
                which is already up).
  checkout      fetching the source. An ephemeral agent starts from an empty volume and
                pays a full clone every build; a shared agent updates the clone it kept.
                On a repository of any size this dominates the difference, so it is its
                own phase rather than part of "everything else".
  cacheRestore  the Prepare Cache stage.
  build         the stages that do the work.
  cleanup       the post actions: archiving, and workspace teardown where it applies.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from netci_gates.jenkins import JenkinsClient  # noqa: E402

BUILD_STAGES = {"Unit Test", "Build", "SBOM", "Vulnerability Scan", "Sign", "Publish"}
CACHE_STAGE = "Prepare Cache"
CHECKOUT_STAGE = "Checkout"
# Post actions: archive and, on a reusable agent, wipe the workspace.
CLEANUP_STAGE = "Declarative: Post Actions"


class Jenkins(JenkinsClient):
    """The shared gate client plus the one call only the benchmark makes.

    Subclassing rather than re-implementing matters here: the crumb Jenkins issues is
    bound to the session cookie it was issued with, and a client that sends the crumb
    without the cookie gets 403 on every POST. That belongs in one place.
    """

    def trigger(self, job: str, parameters: dict[str, str]) -> str:
        query = urllib.parse.urlencode(parameters)
        status, headers, body = self.post(f"/job/{urllib.parse.quote(job)}/buildWithParameters?{query}")
        if not 200 <= status < 400:
            raise SystemExit(f"Jenkins refused the build: HTTP {status} {body[:300].decode(errors='replace')}")
        location = next((value for key, value in headers.items() if key.lower() == "location"), "")
        queue_id = location.rstrip("/").rsplit("/", 1)[-1]
        if not queue_id.isdigit():
            raise SystemExit(f"Jenkins did not queue the build: {location!r}")
        return queue_id


def agent_pod_running_at(kube_context: str, namespace: str, deadline: float) -> float | None:
    """When the first ephemeral agent pod reached Running, or None if none appeared."""

    while time.time() < deadline:
        result = subprocess.run(
            [
                "kubectl", "--context", kube_context, "get", "pods", "-n", namespace,
                "-l", "netci.io/agent=ephemeral", "-o", "json",
            ],
            capture_output=True, text=True, check=False,
        )
        if result.returncode == 0:
            for pod in json.loads(result.stdout or "{}").get("items", []):
                if pod["status"].get("phase") == "Running":
                    return time.time()
        time.sleep(1)
    return None


def stage_durations(jenkins: Jenkins, job: str, number: int) -> dict[str, float]:
    """Read the stage graph Jenkins recorded for the build."""

    try:
        described = jenkins.get_json(f"/job/{urllib.parse.quote(job)}/{number}/wfapi/describe")
    except (urllib.error.URLError, OSError, ValueError):
        return {}
    return {
        str(stage.get("name")): float(stage.get("durationMillis", 0)) / 1000.0
        for stage in described.get("stages", [])
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default=os.getenv("JENKINS_A_URL", ""))
    parser.add_argument("--username", default=os.getenv("JENKINS_A_USERNAME", "admin"))
    parser.add_argument("--token", default=os.getenv("JENKINS_A_API_TOKEN", "change-me-local-only"))
    parser.add_argument("--job", required=True)
    parser.add_argument("--label", required=True, help="agent label, e.g. netci-ephemeral or netci-shared")
    parser.add_argument("--git-url", default=os.getenv("NETCI_GIT_URL", ""))
    parser.add_argument("--branch", default=os.getenv("NETCI_GIT_BRANCH", "main"))
    parser.add_argument("--kube-context", default=os.getenv("NETCI_KUBE_CONTEXT", "kind-netci-local"))
    parser.add_argument("--namespace", default=os.getenv("JENKINS_AGENT_NAMESPACE", "netci-build"))
    parser.add_argument("--timeout", type=float, default=float(os.getenv("NETCI_CI_TIMEOUT", "900")))
    # Kept short on purpose: the comparison is between two agents, not two toolchains,
    # and the later stages add minutes of registry and scanner time to both sides alike.
    parser.add_argument("--stages", default=os.getenv("NETCI_BENCHMARK_STAGES", "unit-test,build"))
    # Per-project isolation (ADR-030): create the pod in this namespace and mount this
    # claim. The pod is then watched there rather than in the cloud's default namespace.
    parser.add_argument("--isolation-namespace", default="")
    parser.add_argument("--cache-claim", default="")
    parser.add_argument("--service-account", default="jenkins-agent")
    arguments = parser.parse_args()
    if arguments.isolation_namespace:
        arguments.namespace = arguments.isolation_namespace

    if not arguments.url:
        print("set --url or JENKINS_A_URL", file=sys.stderr)
        return 2

    jenkins = Jenkins(arguments.url, arguments.username, arguments.token)

    queued_at = time.time()
    # The same coordinates netCI hands its own jobs. A build agent has no route to a
    # public registry, so without these the base image is an unqualified short name that
    # buildah cannot resolve -- and the benchmark would be measuring a failing build.
    queue_id = jenkins.trigger(
        arguments.job,
        {
            "NETCI_AGENT_LABEL": "" if arguments.label == "netci-ephemeral" and arguments.isolation_namespace else arguments.label,
            "NETCI_BUILD_NAMESPACE": arguments.isolation_namespace,
            "NETCI_BUILD_SERVICE_ACCOUNT": arguments.service_account if arguments.isolation_namespace else "",
            "NETCI_BUILD_CACHE_CLAIM": arguments.cache_claim if arguments.isolation_namespace else "",
            "GIT_URL": arguments.git_url,
            "GIT_BRANCH": arguments.branch,
            "COMMIT_SHA": arguments.branch,
            "NETCI_TEMPLATE": "container-ci-cd-v1",
            "NETCI_STAGES": arguments.stages,
            "NETCI_BASE_IMAGE": os.getenv("NETCI_BUILD_BASE_IMAGE", ""),
            "REGISTRY_PUSH_HOST": os.getenv("NETCI_REGISTRY_PUSH_HOST", ""),
            "REGISTRY_PULL_HOST": os.getenv("NETCI_REGISTRY_PULL_HOST", ""),
            "NETCI_TRIVY_DB_REPOSITORY": os.getenv("NETCI_TRIVY_DB_REPOSITORY", ""),
        },
    )

    provisioning_seconds = 0.0
    if arguments.label != "netci-shared":
        running_at = agent_pod_running_at(
            arguments.kube_context, arguments.namespace, deadline=queued_at + arguments.timeout
        )
        provisioning_seconds = round((running_at or queued_at) - queued_at, 3)

    # Wait for the queue item to become a build.
    number = None
    deadline = queued_at + arguments.timeout
    while time.time() < deadline:
        item = jenkins.get_json(f"/queue/item/{queue_id}/api/json")
        if item.get("cancelled"):
            print(f"NETCI_BUILD_ERROR=queue item {queue_id} cancelled", file=sys.stderr)
            return 1
        executable = item.get("executable")
        if isinstance(executable, dict) and executable.get("number") is not None:
            number = int(executable["number"])
            break
        time.sleep(1)
    if number is None:
        print("NETCI_BUILD_ERROR=queue item never started", file=sys.stderr)
        return 1
    started_at = time.time()

    result = None
    while time.time() < deadline:
        build = jenkins.get_json(f"/job/{urllib.parse.quote(arguments.job)}/{number}/api/json")
        if not build.get("building") and build.get("result"):
            result = str(build["result"])
            break
        time.sleep(2)
    finished_at = time.time()
    if result is None:
        print("NETCI_BUILD_ERROR=build did not finish in time", file=sys.stderr)
        return 1

    console_text = jenkins.get_text(
        f"/job/{urllib.parse.quote(arguments.job)}/{number}/consoleText", timeout=60
    )
    cache_hit = "NETCI_CACHE=hit" in console_text

    stages = stage_durations(jenkins, arguments.job, number)
    checkout_seconds = round(stages.get(CHECKOUT_STAGE, 0.0), 3)
    cache_restore_seconds = round(stages.get(CACHE_STAGE, 0.0), 3)
    build_seconds = round(sum(value for name, value in stages.items() if name in BUILD_STAGES), 3)
    cleanup_seconds = round(stages.get(CLEANUP_STAGE, 0.0), 3)
    # Whatever is left is reported rather than folded into a named phase: silently adding
    # an unrecognised stage to `cleanup` is how an 18-second checkout came to be reported
    # as cleanup time, which pointed the benchmark's conclusion at the wrong cost.
    named = {CHECKOUT_STAGE, CACHE_STAGE, CLEANUP_STAGE} | BUILD_STAGES
    other_seconds = round(sum(value for name, value in stages.items() if name not in named), 3)

    print(console_text[-4000:])
    print(
        "NETCI_TIMING_JSON="
        + json.dumps(
            {
                "queueSeconds": round(started_at - queued_at, 3),
                "provisioningSeconds": provisioning_seconds,
                "checkoutSeconds": checkout_seconds,
                "otherSeconds": other_seconds,
                "cacheRestoreSeconds": cache_restore_seconds,
                "buildSeconds": build_seconds,
                "cleanupSeconds": cleanup_seconds,
                "cacheHit": cache_hit,
                "totalSeconds": round(finished_at - queued_at, 3),
                "result": result,
                "job": arguments.job,
                "build": number,
                "label": arguments.label,
            }
        )
    )
    return 0 if result == "SUCCESS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
