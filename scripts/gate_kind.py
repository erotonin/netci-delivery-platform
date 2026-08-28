#!/usr/bin/env python3
"""kind gate: bring up the build cluster and prove its isolation guarantees.

Creating the cluster is the easy half. The half worth asserting is that the namespaces
exist, that the Jenkins controller's RBAC is scoped to the build namespace only, that no
service account mounts a token by default, and that nothing was granted cluster-admin --
because those are the properties ADR-007 claims and a screenshot cannot show.

    python scripts/gate_kind.py
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from netci_gates.evidence import (  # noqa: E402
    PROJECT_ROOT,
    EvidenceRecorder,
    GateFailure,
    require_tools,
    run_gate,
)

INFRA = PROJECT_ROOT / "infra" / "kind"
REQUIRED_NAMESPACES = ("netci-build", "dev", "staging", "prod")


def kubectl(recorder: EvidenceRecorder, name: str, *arguments: str, expect_success: bool = True):
    context = recorder.context["kubeContext"]
    return recorder.run(name, ["kubectl", "--context", context, *arguments], expect_success=expect_success)


def kubectl_json(recorder: EvidenceRecorder, name: str, *arguments: str) -> dict:
    result = kubectl(recorder, name, *arguments, "-o", "json")
    return json.loads(result.stdout or "{}")


def wait_for_nodes(recorder: EvidenceRecorder, expected: int, *, attempts: int = 60) -> list[dict]:
    for _ in range(attempts):
        nodes = kubectl_json(recorder, "wait-for-nodes", "get", "nodes").get("items", [])
        ready = [
            node
            for node in nodes
            if any(c["type"] == "Ready" and c["status"] == "True" for c in node["status"]["conditions"])
        ]
        if len(ready) >= expected:
            return ready
        time.sleep(5)
    raise GateFailure(f"only {len(ready)}/{expected} kind nodes became Ready")


def gate(recorder: EvidenceRecorder) -> None:
    require_tools(recorder, "kind", "kubectl", "docker")
    cluster = recorder.context["cluster"]

    existing = recorder.run("list-clusters", ["kind", "get", "clusters"], expect_success=False)
    if cluster in existing.stdout.split():
        recorder.record("reuse-cluster", {"cluster": cluster, "note": "cluster already exists; not recreated"})
    else:
        recorder.run(
            "create-cluster",
            ["kind", "create", "cluster", "--config", str(INFRA / "kind-config.yaml"), "--wait", "180s"],
            timeout=900,
        )

    nodes = wait_for_nodes(recorder, expected=2)
    recorder.check_equal("the cluster has a control plane and a worker", len(nodes), 2)
    recorder.check(
        "every node reports Ready",
        all(
            any(c["type"] == "Ready" and c["status"] == "True" for c in node["status"]["conditions"])
            for node in nodes
        ),
        detail=[node["metadata"]["name"] for node in nodes],
    )

    kubectl(recorder, "apply-namespaces", "apply", "-f", str(INFRA / "namespaces.yaml"))
    kubectl(recorder, "apply-rbac", "apply", "-f", str(INFRA / "jenkins-agent-rbac.yaml"))

    namespaces = {
        item["metadata"]["name"] for item in kubectl_json(recorder, "get-namespaces", "get", "namespaces")["items"]
    }
    missing = [name for name in REQUIRED_NAMESPACES if name not in namespaces]
    recorder.check("the build and environment namespaces exist", not missing, detail=missing)

    # ------------------------------------------------------------ isolation checks
    accounts = kubectl_json(recorder, "get-service-accounts", "get", "serviceaccounts", "-n", "netci-build")
    managed = {
        item["metadata"]["name"]: item
        for item in accounts["items"]
        if item["metadata"]["name"] in {"jenkins-controller", "jenkins-agent"}
    }
    recorder.check_equal("both build service accounts exist", sorted(managed), ["jenkins-agent", "jenkins-controller"])
    for name, account in managed.items():
        recorder.check_equal(
            f"{name} does not auto-mount a Kubernetes API token",
            account.get("automountServiceAccountToken"),
            False,
        )

    role = kubectl_json(recorder, "get-role", "get", "role", "jenkins-controller", "-n", "netci-build")
    resources = sorted({resource for rule in role["rules"] for resource in rule["resources"]})
    verbs = sorted({verb for rule in role["rules"] for verb in rule["verbs"]})
    recorder.check(
        "the controller Role only touches pods in the build namespace",
        all(resource.startswith("pods") for resource in resources),
        detail=resources,
    )
    recorder.check("the controller Role grants no wildcard verb", "*" not in verbs, detail=verbs)

    bindings = kubectl_json(recorder, "get-cluster-role-bindings", "get", "clusterrolebindings")
    offenders = [
        item["metadata"]["name"]
        for item in bindings["items"]
        if item.get("roleRef", {}).get("name") == "cluster-admin"
        and any(
            subject.get("namespace") == "netci-build" or str(subject.get("name", "")).startswith("jenkins")
            for subject in item.get("subjects") or []
        )
    ]
    recorder.check("nothing in the build namespace was granted cluster-admin", not offenders, detail=offenders)

    recorder.record(
        "cluster-summary",
        {
            "cluster": cluster,
            "nodes": [node["metadata"]["name"] for node in nodes],
            "kubeletVersions": sorted({node["status"]["nodeInfo"]["kubeletVersion"] for node in nodes}),
            "namespaces": sorted(namespaces & set(REQUIRED_NAMESPACES)),
        },
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--cluster", default=os.getenv("NETCI_KIND_CLUSTER", "netci-local"))
    arguments = parser.parse_args()

    recorder = EvidenceRecorder(
        "kind-cluster",
        description="kind build cluster bootstrapped, with namespace, RBAC scope and "
        "service-account-token isolation asserted.",
    )
    recorder.context.update({"cluster": arguments.cluster, "kubeContext": f"kind-{arguments.cluster}"})
    return run_gate(recorder, gate)


if __name__ == "__main__":
    raise SystemExit(main())
