#!/usr/bin/env python3
"""JCasC rebuild gate: destroy a controller and prove Git brings it back identically.

The claim in ADR-006 is that a controller is reproducible from source, not from a
backup or from someone's memory of which boxes were ticked. The only way to test that
is destructively: capture a fingerprint of the live configuration, delete the container
and its JENKINS_HOME, rebuild from the same JCasC files, and compare.

The gate also checks the part that is easy to forget -- netCI's own job is *not* part of
JCasC, and must be re-provisioned by netCI on the next run rather than surviving in
controller state.

    python scripts/gate_jenkins_rebuild.py --controller a
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from netci_gates.jenkins import JenkinsClient  # noqa: E402
from netci_gates.evidence import (  # noqa: E402
    PROJECT_ROOT,
    EvidenceRecorder,
    GateFailure,
    require_tools,
    run_gate,
)


def wait_until(condition, *, attempts: int, delay: float) -> bool:
    for _ in range(attempts):
        if condition():
            return True
        time.sleep(delay)
    return False


def gate(recorder: EvidenceRecorder) -> None:
    require_tools(recorder, "docker")
    context = recorder.context
    container = f"jenkins-{context['controller']}"
    jenkins = JenkinsClient(context["url"], context["username"], context["token"])

    recorder.check(f"{container} is reachable before the drill", jenkins.reachable(), detail=context["url"])

    before = jenkins.fingerprint()
    recorder.record("fingerprint-before", before)
    recorder.check("JCasC exported a configuration to compare against", bool(before["jcascExportSha256"]))
    recorder.check_equal("the controller runs no builds itself", before["numExecutors"], 0)
    recorder.check(
        "the JCasC seed job is present before the drill", "netci-seed" in before["jobs"], detail=before["jobs"]
    )
    recorder.check(
        "the ephemeral agent cloud is live before the drill",
        any("Kubernetes" in item for item in before["ephemeralLabelClouds"]),
        detail=before["ephemeralLabelClouds"],
    )
    recorder.check(
        "the credentials JCasC declares are present",
        {"netci-kind-token", "netci-pipeline-api-key"} <= set(before["credentialIds"]),
        detail=before["credentialIds"],
    )

    # A job netCI created is deliberately *not* in JCasC. It must not survive, and it
    # must not need to: netCI reconciles it on the next run.
    netci_jobs = [name for name in before["jobs"] if name.startswith("netci-") and name != "netci-seed"]
    recorder.record("netci-managed-jobs-before", {"jobs": netci_jobs})

    inspected = recorder.run(
        "inspect-before", ["docker", "inspect", container, "--format", "{{.Id}} {{.Config.Image}}"]
    )
    original_id = inspected.stdout.split()[0]

    # ------------------------------------------------------------------ destroy
    recorder.run("destroy-controller", ["docker", "rm", "-f", container])
    gone = wait_until(lambda: not jenkins.reachable(timeout=2), attempts=30, delay=2)
    recorder.check(f"{container} is really gone", gone)
    removed = recorder.run(
        "confirm-container-removed", ["docker", "ps", "-a", "--filter", f"name=^{container}$", "--format", "{{.Names}}"],
    )
    recorder.check(
        "no container and therefore no JENKINS_HOME survived",
        container not in removed.stdout.split(),
        detail=removed.stdout.strip(),
    )

    # ------------------------------------------------------------------ rebuild
    started = time.time()
    recorder.run(
        "rebuild-from-jcasc",
        ["bash", str(PROJECT_ROOT / "scripts" / "jenkins_lab.sh"), "recreate", context["controller"]],
        timeout=900,
    )
    back = wait_until(jenkins.reachable, attempts=120, delay=2)
    recorder.check(f"{container} came back from configuration alone", back)
    if not back:
        raise GateFailure(f"{container} did not return after the rebuild")
    rebuild_seconds = round(time.time() - started, 1)
    recorder.record("rebuild-duration", {"seconds": rebuild_seconds})

    rebuilt = recorder.run(
        "inspect-after", ["docker", "inspect", container, "--format", "{{.Id}}"]
    )
    recorder.check(
        "the controller is a genuinely new container, not a restarted one",
        rebuilt.stdout.strip() != original_id,
        detail={"before": original_id[:12], "after": rebuilt.stdout.strip()[:12]},
    )

    after = jenkins.fingerprint()
    recorder.record("fingerprint-after", after)

    # ------------------------------------------------------------------ compare
    recorder.check_equal("the plugin set and versions are identical", after["plugins"], before["plugins"])
    recorder.check_equal(
        "the exported configuration is identical once per-instance secrets and generated ids are normalized",
        after["jcascNormalizedSha256"],
        before["jcascNormalizedSha256"],
    )
    recorder.check(
        "the encrypted secret material was re-keyed, as a fresh JENKINS_HOME must",
        after["jcascExportSha256"] != before["jcascExportSha256"],
        detail={"before": before["jcascExportSha256"][:16], "after": after["jcascExportSha256"][:16]},
    )
    recorder.check_equal("the executor policy came back", after["numExecutors"], before["numExecutors"])
    recorder.check_equal(
        "the agent cloud came back", after["ephemeralLabelClouds"], before["ephemeralLabelClouds"]
    )
    recorder.check_equal("the declared credentials came back", after["credentialIds"], before["credentialIds"])
    recorder.check("the JCasC seed job came back", "netci-seed" in after["jobs"], detail=after["jobs"])

    recorder.check(
        "jobs netCI provisioned did not survive, because they are not part of JCasC",
        not [name for name in after["jobs"] if name in netci_jobs],
        detail={"before": netci_jobs, "after": after["jobs"]},
    )
    recorder.record(
        "rebuild-summary",
        {
            "controller": container,
            "rebuildSeconds": rebuild_seconds,
            "plugins": len(after["plugins"]),
            "jcascNormalizedSha256": after["jcascNormalizedSha256"],
            "note": "netCI reconciles its own jobs on the next pipeline run; see gate jenkins-ci-loop",
        },
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--controller", default=os.getenv("NETCI_REBUILD_CONTROLLER", "a"), choices=["a", "b"])
    arguments = parser.parse_args()

    prefix = f"JENKINS_{arguments.controller.upper()}"
    url = os.getenv(f"{prefix}_URL", "").strip()
    if not url:
        print(f"set {prefix}_URL (see scripts/jenkins_lab.sh up)", file=sys.stderr)
        return 2

    recorder = EvidenceRecorder(
        "jenkins-rebuild",
        description="A controller is deleted with its JENKINS_HOME and rebuilt from JCasC; "
        "plugins, exported configuration, clouds and credentials are compared before and after.",
    )
    recorder.context.update(
        {
            "controller": arguments.controller,
            "url": url.rstrip("/"),
            "username": os.getenv(f"{prefix}_USERNAME", "admin"),
            "token": os.getenv(f"{prefix}_API_TOKEN", "change-me-local-only"),
        }
    )
    return run_gate(recorder, gate)


if __name__ == "__main__":
    raise SystemExit(main())
