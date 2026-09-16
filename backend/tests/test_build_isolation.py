"""Per-project build isolation: what is provisioned, and that a build is never
dispatched without it."""

from __future__ import annotations

import json
import subprocess
from uuid import UUID, uuid4

import pytest

from app.adapters import build_isolation as bi
from app.adapters.ci_launcher import CiLaunchError, CiLaunchRequest, JenkinsCiLauncher, LaunchedCi
from app.adapters.interfaces import JenkinsRun
from app.adapters.jenkins_router import JenkinsController, JenkinsRouter


APP = UUID("8c7dc688-ceac-4eff-91ae-d7deec2e8d86")


def test_namespace_is_dns_safe_stable_and_tied_to_the_application():
    a = bi.namespace_for(APP, "hello-container")
    assert a == bi.namespace_for(APP, "hello-container")
    assert a.startswith("netci-build-hello-container-")
    assert len(a) <= 63
    assert a != bi.namespace_for(uuid4(), "hello-container"), "same name, other application: other namespace"
    long_name = "a" * 80
    assert len(bi.namespace_for(APP, long_name)) <= 63
    assert bi.namespace_for(APP, "Hello_World!").startswith("netci-build-hello-world-")


def test_manifests_bind_the_controller_only_in_the_project_namespace_and_deny_ingress(tmp_path):
    kubeconfig = tmp_path / "kubeconfig"; kubeconfig.write_text("apiVersion: v1\n")
    prov = bi.KubernetesBuildIsolation(kubeconfig=str(kubeconfig), controller_service_account="netci-build/jenkins-controller", cache_size="3Gi", storage_class="fast")
    items = {(m["kind"], m["metadata"]["name"]): m for m in prov.manifests(APP, "hello-container")}
    namespace = bi.namespace_for(APP, "hello-container")
    assert ("Namespace", namespace) in items
    assert items[("Namespace", namespace)]["metadata"]["labels"]["netci.io/application"] == str(APP)
    sa = items[("ServiceAccount", "jenkins-agent")]
    assert sa["automountServiceAccountToken"] is False and sa["metadata"]["namespace"] == namespace
    binding = items[("RoleBinding", "jenkins-controller")]
    assert binding["metadata"]["namespace"] == namespace
    assert binding["subjects"] == [{"kind": "ServiceAccount", "name": "jenkins-controller", "namespace": "netci-build"}]
    assert binding["roleRef"]["kind"] == "Role", "a namespaced Role, never a ClusterRole"
    policy = items[("NetworkPolicy", "build-pods")]
    assert policy["spec"]["policyTypes"] == ["Ingress"] and policy["spec"]["ingress"] == []
    pvc = items[("PersistentVolumeClaim", "netci-cache")]
    assert pvc["spec"]["resources"]["requests"]["storage"] == "3Gi"
    assert pvc["spec"]["storageClassName"] == "fast"
    assert all(m["metadata"].get("namespace", m["metadata"]["name"]) == namespace for m in items.values())


def test_ensure_applies_the_manifests_and_reads_the_claim_back(tmp_path, monkeypatch):
    kubeconfig = tmp_path / "kubeconfig"; kubeconfig.write_text("apiVersion: v1\n")
    prov = bi.KubernetesBuildIsolation(kubeconfig=str(kubeconfig), controller_service_account="netci-build/jenkins-controller")
    calls = []

    def fake_run(command, input=None, **kwargs):
        calls.append((command, input))
        if command[3] == "apply":
            assert command[1:3] == ["--kubeconfig", str(kubeconfig)]
            docs = [json.loads(part) for part in input.split("\n---\n")]
            assert {d["kind"] for d in docs} == {"Namespace", "ServiceAccount", "Role", "RoleBinding", "NetworkPolicy", "PersistentVolumeClaim"}
            return subprocess.CompletedProcess(command, 0, "applied", "")
        return subprocess.CompletedProcess(command, 0, json.dumps({"status": {"phase": "Bound"}}), "")

    monkeypatch.setattr(bi.subprocess, "run", fake_run)
    isolation = prov.ensure(APP, "hello-container")
    assert isolation.namespace == bi.namespace_for(APP, "hello-container")
    assert isolation.as_parameters() == {
        "NETCI_BUILD_NAMESPACE": isolation.namespace,
        "NETCI_BUILD_SERVICE_ACCOUNT": "jenkins-agent",
        "NETCI_BUILD_CACHE_CLAIM": "netci-cache",
    }
    assert len(calls) == 2


def test_ensure_fails_closed_when_the_cluster_refuses(tmp_path, monkeypatch):
    kubeconfig = tmp_path / "kubeconfig"; kubeconfig.write_text("apiVersion: v1\n")
    prov = bi.KubernetesBuildIsolation(kubeconfig=str(kubeconfig), controller_service_account="netci-build/jenkins-controller")
    monkeypatch.setattr(bi.subprocess, "run", lambda command, **kw: subprocess.CompletedProcess(command, 1, "", "forbidden: namespaces"))
    with pytest.raises(bi.BuildIsolationError, match="forbidden"):
        prov.ensure(APP, "hello-container")


class _Adapter:
    def __init__(self):
        self.requests = []

    def health_check(self):
        return True

    def create_or_update_job(self, application_id, template_id):
        return "job"

    def trigger_ci_run(self, job_name, request, callback_token=""):
        self.requests.append(request)
        return JenkinsRun(run_id="job#1", status="running", console_url="http://j/1")


class _RefusingProvisioner:
    mode = "kubernetes"

    def ensure(self, application_id, application_name):
        raise bi.BuildIsolationError("cluster unreachable")

    def describe(self):
        return {"mode": "kubernetes", "ready": False}


class _Provisioner:
    mode = "kubernetes"

    def ensure(self, application_id, application_name):
        return bi.BuildIsolation(namespace=bi.namespace_for(application_id, application_name), service_account="jenkins-agent", cache_claim="netci-cache")

    def describe(self):
        return {"mode": "kubernetes", "ready": True}


def _request():
    return CiLaunchRequest(
        application_id=APP, application_name="hello-container", repository_url="http://git/netci.git",
        pipeline_template="container-ci-cd-v1", runtime="docker", stages=("build",), pipeline_run_id=uuid4(),
        commit_sha="a" * 40, branch="main", environment="dev", correlation_id="c", parameters={},
    )


def test_a_build_is_not_dispatched_when_isolation_cannot_be_established():
    adapter = _Adapter()
    launcher = JenkinsCiLauncher(JenkinsRouter([JenkinsController("jenkins-a", capabilities={"docker"})]), {"jenkins-a": adapter}, isolation=_RefusingProvisioner())
    with pytest.raises(CiLaunchError, match="build isolation unavailable"):
        launcher.launch(_request())
    assert adapter.requests == [], "nothing reached Jenkins"


def test_the_launch_carries_the_project_namespace_to_jenkins():
    adapter = _Adapter()
    launcher = JenkinsCiLauncher(JenkinsRouter([JenkinsController("jenkins-a", capabilities={"docker"})]), {"jenkins-a": adapter}, isolation=_Provisioner())
    launched = launcher.launch(_request())
    assert isinstance(launched, LaunchedCi)
    assert adapter.requests[0].isolation.namespace == bi.namespace_for(APP, "hello-container")


def test_isolation_must_be_chosen_explicitly_outside_local_mode(monkeypatch):
    monkeypatch.setenv("NETCI_ENVIRONMENT", "production")
    monkeypatch.delenv("NETCI_BUILD_ISOLATION", raising=False)
    with pytest.raises(ValueError, match="must be set outside local mode"):
        bi.build_isolation_provisioner()
    monkeypatch.setenv("NETCI_BUILD_ISOLATION", "none")
    assert bi.build_isolation_provisioner().mode == "none"
    monkeypatch.setenv("NETCI_BUILD_ISOLATION", "kubernetes")
    monkeypatch.setenv("NETCI_BUILD_CLUSTER_KUBECONFIG", "/nonexistent/kubeconfig")
    with pytest.raises(ValueError, match="kubeconfig"):
        bi.build_isolation_provisioner()


# --------------------------------------------------------- controller drift


class _FingerprintAdapter(_Adapter):
    def __init__(self, jcasc, plugins, jobs):
        super().__init__()
        self.fp = {"jcascNormalizedSha256": jcasc, "pluginsSha256": plugins, "pluginCount": 3, "jobs": jobs}

    def configuration_fingerprint(self):
        return dict(self.fp)


def _launcher(adapters):
    return JenkinsCiLauncher(JenkinsRouter([JenkinsController(cid, capabilities={"docker"}) for cid in adapters]), adapters, isolation=_Provisioner())


def test_identical_controllers_report_no_drift():
    launcher = _launcher({
        "jenkins-a": _FingerprintAdapter("c1", "p1", ["netci-app-1", "netci-benchmark"]),
        "jenkins-b": _FingerprintAdapter("c1", "p1", ["netci-app-2"]),
    })
    report = launcher.controller_drift()
    assert report["drift"] is False and report["differing"] == []


def test_a_hand_edited_controller_is_reported_with_what_differs():
    launcher = _launcher({
        "jenkins-a": _FingerprintAdapter("c1", "p1", ["ops-manual-job"]),
        "jenkins-b": _FingerprintAdapter("c2", "p1", []),
    })
    report = launcher.controller_drift()
    assert report["drift"] is True
    assert report["differing"] == ["jcascNormalizedSha256", "jobs"]


def test_an_unreachable_controller_counts_as_drift():
    class Down(_FingerprintAdapter):
        def configuration_fingerprint(self):
            raise OSError("connection refused")

    launcher = _launcher({"jenkins-a": _FingerprintAdapter("c1", "p1", []), "jenkins-b": Down("c1", "p1", [])})
    report = launcher.controller_drift()
    assert report["drift"] is True and "jenkins-b" in report["unreachable"]


def test_normalization_masks_per_controller_and_runtime_noise_but_not_configuration():
    """Two controllers built from the same git differ in identity and in transient
    state (pods currently running); a real difference -- a cloud, a credential, a
    plugin -- must still change the fingerprint."""
    from app.adapters.jenkins_http import JenkinsHttpAdapter

    normalize = JenkinsHttpAdapter._normalize_jcasc
    a = 'jenkins:\n  labelAtoms:\n  - name: "built-in"\n  - name: "netci-benchmark-33-t2254-fdgsp-cr7bp"\n  - name: "netci-benchmark_33-t2254"\n  systemMessage: "controller A"\n  clouds:\n  - kubernetes:\n      jenkinsUrl: "http://jenkins-a:8080"\n      credentialsId: "netci-kind-token"\n      libraryPath: "."\n'
    b = 'jenkins:\n  labelAtoms:\n  - name: "built-in"\n  systemMessage: "controller B"\n  clouds:\n  - kubernetes:\n      jenkinsUrl: "http://jenkins-b:8080"\n      credentialsId: "netci-kind-token"\n'
    assert normalize(a) == normalize(b)
    c = b.replace('credentialsId: "netci-kind-token"', 'credentialsId: "someone-elses-token"')
    assert normalize(c) != normalize(b)


# ------------------------------------------------------- traffic router fail-closed


def test_canary_weights_are_refused_when_no_router_can_apply_them(monkeypatch):
    """The in-memory router used to be the production default: a 200 with a weight that
    routed nothing. Outside local mode the choice must be explicit, and `none` refuses."""
    from app import traffic

    monkeypatch.setenv("NETCI_ENVIRONMENT", "production")
    monkeypatch.delenv("NETCI_TRAFFIC_ROUTER", raising=False)
    router = traffic.build_traffic_router()
    assert isinstance(router, traffic.UnconfiguredTrafficRouter)
    with pytest.raises(traffic.TrafficRoutingUnavailable):
        router.set_traffic_weight("app", "prod", 10)
    assert router.get_routing_status("app", "prod")["status"] == "not_configured"
    monkeypatch.setenv("NETCI_TRAFFIC_ROUTER", "memory")
    with pytest.raises(ValueError, match="local-only"):
        traffic.build_traffic_router()
    monkeypatch.setenv("NETCI_ENVIRONMENT", "local")
    assert isinstance(traffic.build_traffic_router(), traffic.InMemoryTrafficRoutingAdapter)
