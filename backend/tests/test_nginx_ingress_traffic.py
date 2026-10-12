"""The ingress-nginx router moves a weight by patching the canary Ingress and reads it back."""

from __future__ import annotations

import json
import subprocess

import pytest

from backend.app.adapters import nginx_ingress_traffic as mod

APP = "8c7dc688-ceac-4eff-91ae-d7deec2e8d86"


class FakeCluster:
    """Just enough of `kubectl get/patch ingress` to exercise the router's logic."""

    def __init__(self, ingresses: list[dict] | None = None, services: list[dict] | None = None, endpoints: dict[str, list[str]] | None = None) -> None:
        self.ingresses = ingresses or []
        self.services = services or []
        self.endpoints = endpoints or {}
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(args)
        if args[:2] == ["get", "service"]:
            if "-l" in args:
                selector = dict(part.split("=", 1) for part in args[args.index("-l") + 1].split(","))
                items = [s for s in self.services if all(s["metadata"]["labels"].get(k) == v for k, v in selector.items())]
                return subprocess.CompletedProcess(args, 0, json.dumps({"items": items}), "")
            match = [s for s in self.services if s["metadata"]["name"] == args[2]]
            return subprocess.CompletedProcess(args, 0 if match else 1, json.dumps(match[0]) if match else "", "" if match else "not found")
        if args[:2] == ["get", "endpoints"]:
            ips = self.endpoints.get(args[2], [])
            return subprocess.CompletedProcess(args, 0, json.dumps({"subsets": [{"addresses": [{"ip": ip} for ip in ips]}] if ips else []}), "")
        if args[:2] == ["get", "ingress"]:
            selector = dict(part.split("=", 1) for part in args[args.index("-l") + 1].split(","))
            items = [
                item for item in self.ingresses
                if all(item["metadata"]["labels"].get(key) == value for key, value in selector.items())
            ]
            return subprocess.CompletedProcess(args, 0, json.dumps({"items": items}), "")
        if args[:2] == ["patch", "ingress"]:
            name, namespace = args[2], args[args.index("-n") + 1]
            patch = json.loads(args[args.index("-p") + 1])
            for item in self.ingresses:
                if item["metadata"]["name"] == name and item["metadata"]["namespace"] == namespace:
                    if "spec" in patch:
                        item["spec"]["rules"] = patch["spec"]["rules"]
                    annotations = item["metadata"].setdefault("annotations", {})
                    for key, value in (patch.get("metadata") or {}).get("annotations", {}).items():
                        if value is None:
                            annotations.pop(key, None)
                        else:
                            annotations[key] = value
                    return subprocess.CompletedProcess(args, 0, "patched", "")
            return subprocess.CompletedProcess(args, 1, "", "not found")
        raise AssertionError(f"unexpected kubectl {args}")


def canary_ingress(weight: int = 10, namespace: str = "prod") -> dict:
    return {
        "metadata": {
            "name": "hello-kubernetes-canary-sample-kubernetes-app-ingress",
            "namespace": namespace,
            "labels": {mod.APPLICATION_LABEL: APP, mod.ENVIRONMENT_LABEL: "prod", mod.TRACK_LABEL: "canary"},
            "annotations": {"nginx.ingress.kubernetes.io/canary": "true", "nginx.ingress.kubernetes.io/canary-weight": str(weight)},
        },
        "spec": {"rules": [{"host": "hello-kubernetes.prod.netci.local"}]},
    }


def router(cluster: FakeCluster) -> mod.NginxIngressTrafficRouter:
    r = mod.NginxIngressTrafficRouter(kubeconfig="/dev/null")
    r._kubectl = cluster  # type: ignore[method-assign]
    return r


def test_weight_is_patched_onto_the_canary_ingress_and_read_back():
    cluster = FakeCluster([canary_ingress(10)])
    status = router(cluster).set_traffic_weight(APP, "prod", 25)
    assert status["canaryWeight"] == 25 and status["baselineWeight"] == 75
    assert status["ingress"] == "prod/hello-kubernetes-canary-sample-kubernetes-app-ingress"
    assert status["hosts"] == ["hello-kubernetes.prod.netci.local"]
    assert cluster.ingresses[0]["metadata"]["annotations"]["nginx.ingress.kubernetes.io/canary-weight"] == "25"


def test_without_a_canary_release_there_is_nothing_to_weight():
    cluster = FakeCluster([])
    with pytest.raises(mod.CanaryIngressMissing):
        router(cluster).set_traffic_weight(APP, "prod", 25)
    status = router(cluster).get_routing_status(APP, "prod")
    assert status["canaryWeight"] == 0 and status["canaryRelease"] == "absent"
    assert not any(call[0] == "patch" for call in cluster.calls)


def test_the_stable_ingress_is_never_the_one_patched():
    stable = canary_ingress(0)
    stable["metadata"]["name"] = "hello-kubernetes-sample-kubernetes-app-ingress"
    stable["metadata"]["labels"][mod.TRACK_LABEL] = "stable"
    stable["metadata"]["annotations"] = {}
    cluster = FakeCluster([stable, canary_ingress(10)])
    router(cluster).set_traffic_weight(APP, "prod", 50)
    assert stable["metadata"]["annotations"] == {}
    assert cluster.ingresses[1]["metadata"]["annotations"]["nginx.ingress.kubernetes.io/canary-weight"] == "50"


def test_two_canary_ingresses_for_one_application_is_refused_not_guessed():
    cluster = FakeCluster([canary_ingress(10), canary_ingress(10, namespace="prod-b")])
    with pytest.raises(RuntimeError, match="more than one canary ingress"):
        router(cluster).set_traffic_weight(APP, "prod", 50)


def test_a_weight_the_cluster_did_not_take_is_an_error_not_a_status():
    cluster = FakeCluster([canary_ingress(10)])
    r = router(cluster)
    original = cluster.__call__

    def patch_is_lost(args):
        if args[:2] == ["patch", "ingress"]:
            return subprocess.CompletedProcess(args, 0, "patched", "")
        return original(args)

    r._kubectl = patch_is_lost  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="reports canary weight 10 after setting 25"):
        r.set_traffic_weight(APP, "prod", 25)


def test_rules_become_annotations_and_dropped_rules_are_removed():
    cluster = FakeCluster([canary_ingress(10)])
    r = router(cluster)
    status = r.set_canary_rules(APP, "prod", {"headerName": "X-Canary", "headerValue": "always", "cookie": "canary=1"})
    assert status["canaryRules"] == {"headerName": "X-Canary", "headerValue": "always", "cookie": "canary"}
    status = r.set_canary_rules(APP, "prod", {"cookie": "canary"})
    assert status["canaryRules"] == {"cookie": "canary"}
    annotations = cluster.ingresses[0]["metadata"]["annotations"]
    assert "nginx.ingress.kubernetes.io/canary-by-header" not in annotations
    assert "nginx.ingress.kubernetes.io/canary-by-header-value" not in annotations


@pytest.mark.parametrize("rules", [{"headerName": "X Canary"}, {"headerName": "X-C", "headerValue": 'a"b'}, {"cookie": "a;b"}])
def test_rule_values_that_could_break_the_annotation_are_refused(rules):
    cluster = FakeCluster([canary_ingress(10)])
    with pytest.raises(ValueError):
        router(cluster).set_canary_rules(APP, "prod", rules)
    assert not any(call[0] == "patch" for call in cluster.calls)


def test_a_switch_without_a_stable_ingress_is_refused_not_faked():
    with pytest.raises(RuntimeError, match="expected one stable ingress"):
        router(FakeCluster([canary_ingress(0)])).switch_route(APP, "prod", "green")


def test_label_values_are_validated_before_they_reach_a_selector():
    with pytest.raises(ValueError):
        router(FakeCluster()).get_routing_status("app,netci.io/track=stable", "prod")


def test_builder_requires_a_kubeconfig(monkeypatch, tmp_path):
    monkeypatch.delenv("NETCI_TRAFFIC_KUBECONFIG", raising=False)
    with pytest.raises(ValueError, match="NETCI_TRAFFIC_KUBECONFIG"):
        mod.build_nginx_ingress_router()
    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.write_text("apiVersion: v1\n")
    monkeypatch.setenv("NETCI_TRAFFIC_KUBECONFIG", str(kubeconfig))
    assert mod.build_nginx_ingress_router().kubeconfig == str(kubeconfig)


# ----------------------------------------------------------------- blue/green (ADR-035)


def stable_ingress(backend: str = "hello-kubernetes-sample-kubernetes-app") -> dict:
    return {
        "metadata": {"name": "hello-kubernetes-sample-kubernetes-app-ingress", "namespace": "prod",
                     "labels": {mod.APPLICATION_LABEL: APP, mod.ENVIRONMENT_LABEL: "prod", mod.TRACK_LABEL: "stable"}, "annotations": {}},
        "spec": {"rules": [{"host": "hello-kubernetes.prod.netci.local", "http": {"paths": [
            {"path": "/", "pathType": "Prefix", "backend": {"service": {"name": backend, "port": {"number": 8080}}}}]}}]},
    }


def service(name: str, track: str) -> dict:
    return {"metadata": {"name": name, "namespace": "prod", "labels": {mod.APPLICATION_LABEL: APP, mod.ENVIRONMENT_LABEL: "prod", mod.TRACK_LABEL: track}}}


def colours_cluster(green_ready: bool = True) -> FakeCluster:
    return FakeCluster(
        [stable_ingress()],
        [service("hello-kubernetes-sample-kubernetes-app", "stable"), service("hello-kubernetes-blue-sample-kubernetes-app", "blue"),
         service("hello-kubernetes-green-sample-kubernetes-app", "green")],
        {"hello-kubernetes-green-sample-kubernetes-app": ["10.0.0.5"] if green_ready else [], "hello-kubernetes-blue-sample-kubernetes-app": ["10.0.0.6"]},
    )


def test_switching_points_the_stable_ingress_at_the_colours_service():
    cluster = colours_cluster()
    r = router(cluster)
    assert r.get_routing_status(APP, "prod")["activeColor"] == "stable"
    status = r.switch_route(APP, "prod", "green")
    assert status["activeColor"] == "green"
    assert cluster.ingresses[0]["spec"]["rules"][0]["http"]["paths"][0]["backend"]["service"]["name"] == "hello-kubernetes-green-sample-kubernetes-app"
    assert r.switch_route(APP, "prod", "blue")["activeColor"] == "blue"


def test_a_colour_with_no_ready_endpoints_is_not_switched_to():
    cluster = colours_cluster(green_ready=False)
    with pytest.raises(RuntimeError, match="no ready endpoints"):
        router(cluster).switch_route(APP, "prod", "green")
    assert cluster.ingresses[0]["spec"]["rules"][0]["http"]["paths"][0]["backend"]["service"]["name"] == "hello-kubernetes-sample-kubernetes-app"


def test_a_colour_that_was_never_deployed_is_refused():
    cluster = FakeCluster([stable_ingress()], [service("hello-kubernetes-sample-kubernetes-app", "stable")], {})
    with pytest.raises(RuntimeError, match="deploy the colour first"):
        router(cluster).switch_route(APP, "prod", "green")


def test_only_blue_or_green_are_colours():
    with pytest.raises(ValueError):
        router(colours_cluster()).switch_route(APP, "prod", "purple")
