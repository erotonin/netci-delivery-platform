"""Canary traffic through ingress-nginx (ADR-031).

The canary release (`deploy-kubernetes.yml`, `release_track=canary`) installs a second
Ingress for the stable release's host, annotated as an ingress-nginx canary. The weight
on that Ingress is the only thing that moves traffic; this router changes it and reads
it back, so a weight netCI reports is one nginx has been told, not one a dict remembers.

kubectl rather than a client library, for the reason `build_isolation` gives: the
processes already require it, and a patch with `--type merge` is idempotent.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..traffic import TrafficRoutingAdapter

logger = logging.getLogger(__name__)

APPLICATION_LABEL = "netci.io/application"
TRACK_LABEL = "netci.io/track"
ENVIRONMENT_LABEL = "netci.io/environment"
_ANNOTATION = "nginx.ingress.kubernetes.io/"
_HEADER_NAME = re.compile(r"^[A-Za-z0-9-]{1,64}$")
_COOKIE_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_HEADER_VALUE = re.compile(r"^[^\r\n\"]{0,256}$")
_LABEL_VALUE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9_.-]{0,61}[A-Za-z0-9])?$")


class CanaryIngressMissing(RuntimeError):
    """No canary ingress exists for this application and environment.

    Distinct from `TrafficRoutingUnavailable` (no router at all): the router is there,
    the canary release is not -- the weight is refused because there is nothing yet to
    carry it, which is the case until the canary deployment reports healthy.
    """


class NginxIngressTrafficRouter(TrafficRoutingAdapter):
    mode = "nginx-ingress"

    def __init__(self, *, kubeconfig: str, kubectl: str = "kubectl", timeout_seconds: float = 30.0) -> None:
        self.kubeconfig = kubeconfig
        self.kubectl = kubectl
        self.timeout_seconds = timeout_seconds

    # ------------------------------------------------------------------ adapter

    def set_traffic_weight(self, application_id, environment, canary_weight, baseline_weight=None):
        weight = max(0, min(100, int(canary_weight)))
        ingress = self._canary_ingress(application_id, environment)
        self._annotate(ingress, {_ANNOTATION + "canary": "true", _ANNOTATION + "canary-weight": str(weight)})
        status = self.get_routing_status(application_id, environment)
        if status.get("canaryWeight") != weight:
            raise RuntimeError(
                f"ingress {status.get('ingress')} reports canary weight {status.get('canaryWeight')} after setting {weight}"
            )
        logger.info("ingress-nginx canary %s:%s -> %d%% (%s)", application_id, environment, weight, status["ingress"])
        return status

    def switch_route(self, application_id, environment, active_color):
        """Point the stable Ingress at the colour's Service (ADR-035).

        The colour is a release beside stable (`<release>-blue|green`) with a Service and
        no Ingress. Switching is one patch of the stable Ingress's backend service name;
        it is refused when the colour's Service has no ready endpoints, because a switch
        to nothing is an outage with a green status.
        """

        color = str(active_color).lower()
        if color not in ("blue", "green"):
            raise ValueError(f"invalid route colour {active_color!r}: blue or green")
        ingress = self._stable_ingress(application_id, environment)
        namespace = ingress["metadata"]["namespace"]
        service = self._tracked_service(application_id, environment, color, namespace)
        ready = self._ready_addresses(namespace, service["metadata"]["name"])
        if not ready:
            raise RuntimeError(
                f"colour {color} has no ready endpoints behind {namespace}/{service['metadata']['name']}; not switching"
            )
        name = service["metadata"]["name"]
        patched_rules = []
        for rule in (ingress.get("spec") or {}).get("rules") or []:
            paths = []
            for path in ((rule.get("http") or {}).get("paths") or []):
                backend = dict(path.get("backend") or {})
                svc = dict(backend.get("service") or {})
                svc["name"] = name
                backend["service"] = svc
                paths.append({**path, "backend": backend})
            patched_rules.append({**rule, "http": {**(rule.get("http") or {}), "paths": paths}})
        patch = json.dumps({"spec": {"rules": patched_rules}})
        metadata = ingress["metadata"]
        result = self._kubectl(["patch", "ingress", metadata["name"], "-n", namespace, "--type", "merge", "-p", patch])
        if result.returncode != 0:
            raise RuntimeError(f"cannot switch ingress {namespace}/{metadata['name']}: {(result.stderr or result.stdout).strip()[-300:]}")
        status = self.get_routing_status(application_id, environment)
        if status.get("activeColor") != color:
            raise RuntimeError(f"ingress reports {status.get('activeColor')} after switching to {color}")
        logger.info("ingress-nginx blue/green %s:%s -> %s (%s, %d ready)", application_id, environment, color, name, len(ready))
        return status

    def set_canary_rules(self, application_id, environment, rules):
        header = str(rules.get("header_name") or rules.get("headerName") or "").strip()
        header_value = str(rules.get("header_value") or rules.get("headerValue") or "").strip()
        cookie = str(rules.get("cookie") or "").strip()
        cookie_name = cookie.partition("=")[0].strip()
        if header and not _HEADER_NAME.fullmatch(header):
            raise ValueError("canary header name must be a token of letters, digits and '-'")
        if header_value and not _HEADER_VALUE.fullmatch(header_value):
            raise ValueError("canary header value may not contain quotes or line breaks")
        if cookie_name and not _COOKIE_NAME.fullmatch(cookie_name):
            raise ValueError("canary cookie name must be a token of letters, digits, '_' and '-'")
        ingress = self._canary_ingress(application_id, environment)
        # Absent rules are removed (None deletes the key in a merge patch), so a rule
        # from an earlier request cannot keep steering after the operator dropped it.
        self._annotate(
            ingress,
            {
                _ANNOTATION + "canary-by-header": header or None,
                _ANNOTATION + "canary-by-header-value": (header_value if header and header_value else None),
                _ANNOTATION + "canary-by-cookie": cookie_name or None,
            },
        )
        return self.get_routing_status(application_id, environment)

    def get_routing_status(self, application_id, environment):
        base = {
            "applicationId": str(application_id),
            "environment": str(environment),
            "router": self.mode,
            "activeColor": self._active_color(application_id, environment),
            "updatedAt": datetime.now(timezone.utc).isoformat(),
        }
        try:
            ingress = self._canary_ingress(application_id, environment)
        except CanaryIngressMissing:
            return {**base, "canaryWeight": 0, "baselineWeight": 100, "canaryRules": {}, "canaryRelease": "absent"}
        annotations = (ingress.get("metadata") or {}).get("annotations") or {}
        weight = int(annotations.get(_ANNOTATION + "canary-weight") or 0) if annotations.get(_ANNOTATION + "canary") == "true" else 0
        rules: dict[str, str] = {}
        if annotations.get(_ANNOTATION + "canary-by-header"):
            rules["headerName"] = annotations[_ANNOTATION + "canary-by-header"]
            if annotations.get(_ANNOTATION + "canary-by-header-value"):
                rules["headerValue"] = annotations[_ANNOTATION + "canary-by-header-value"]
        if annotations.get(_ANNOTATION + "canary-by-cookie"):
            rules["cookie"] = annotations[_ANNOTATION + "canary-by-cookie"]
        metadata = ingress.get("metadata") or {}
        hosts = [rule.get("host") for rule in ((ingress.get("spec") or {}).get("rules") or []) if rule.get("host")]
        return {
            **base,
            "canaryWeight": weight,
            "baselineWeight": 100 - weight,
            "canaryRules": rules,
            "canaryRelease": "present",
            "ingress": f"{metadata.get('namespace')}/{metadata.get('name')}",
            "hosts": hosts,
        }

    def describe(self) -> dict[str, Any]:
        probe = self._kubectl(["auth", "can-i", "patch", "ingresses.networking.k8s.io", "--all-namespaces"])
        allowed = probe.returncode == 0 and probe.stdout.strip() == "yes"
        return {
            "mode": self.mode,
            "status": "ready" if allowed else "unavailable",
            "ready": allowed,
            **({} if allowed else {"error": (probe.stderr or probe.stdout).strip()[-300:] or "cannot patch ingresses"}),
        }

    # ------------------------------------------------------------------ kubectl

    def _active_color(self, application_id: str, environment: str) -> str:
        """Which colour the stable Ingress sends to: `blue`/`green`, `stable` when it is
        the stable release's own Service, `unknown` when there is no stable ingress."""

        try:
            ingress = self._stable_ingress(application_id, environment)
        except (RuntimeError, ValueError):
            return "unknown"
        names = {
            (path.get("backend") or {}).get("service", {}).get("name")
            for rule in (ingress.get("spec") or {}).get("rules") or []
            for path in ((rule.get("http") or {}).get("paths") or [])
        }
        names.discard(None)
        if len(names) != 1:
            return "unknown"
        name = names.pop()
        namespace = ingress["metadata"]["namespace"]
        result = self._kubectl(["get", "service", name, "-n", namespace, "-o", "json"])
        if result.returncode != 0:
            return "unknown"
        return str(((json.loads(result.stdout or "{}").get("metadata") or {}).get("labels") or {}).get(TRACK_LABEL) or "unknown")

    def _stable_ingress(self, application_id: str, environment: str) -> dict[str, Any]:
        for value in (str(application_id), str(environment)):
            if not _LABEL_VALUE.fullmatch(value):
                raise ValueError(f"not a label value: {value!r}")
        selector = f"{APPLICATION_LABEL}={application_id},{ENVIRONMENT_LABEL}={environment},{TRACK_LABEL}=stable"
        result = self._kubectl(["get", "ingress", "--all-namespaces", "-l", selector, "-o", "json"])
        if result.returncode != 0:
            raise RuntimeError(f"cannot list stable ingresses: {(result.stderr or result.stdout).strip()[-300:]}")
        items = json.loads(result.stdout or "{}").get("items") or []
        if len(items) != 1:
            raise RuntimeError(f"expected one stable ingress for application {application_id} in {environment}, found {len(items)}")
        return items[0]

    def _tracked_service(self, application_id: str, environment: str, track: str, namespace: str) -> dict[str, Any]:
        selector = f"{APPLICATION_LABEL}={application_id},{ENVIRONMENT_LABEL}={environment},{TRACK_LABEL}={track}"
        result = self._kubectl(["get", "service", "-n", namespace, "-l", selector, "-o", "json"])
        if result.returncode != 0:
            raise RuntimeError(f"cannot list services: {(result.stderr or result.stdout).strip()[-300:]}")
        items = json.loads(result.stdout or "{}").get("items") or []
        if len(items) != 1:
            raise RuntimeError(
                f"colour {track} of application {application_id} in {environment}: expected one Service in {namespace}, found {len(items)} "
                "(deploy the colour first)"
            )
        return items[0]

    def _ready_addresses(self, namespace: str, service: str) -> list[str]:
        result = self._kubectl(["get", "endpoints", service, "-n", namespace, "-o", "json"])
        if result.returncode != 0:
            return []
        addresses: list[str] = []
        for subset in json.loads(result.stdout or "{}").get("subsets") or []:
            addresses.extend(a.get("ip", "") for a in subset.get("addresses") or [])
        return [a for a in addresses if a]

    def _canary_ingress(self, application_id: str, environment: str) -> dict[str, Any]:
        for value in (str(application_id), str(environment)):
            if not _LABEL_VALUE.fullmatch(value):
                raise ValueError(f"not a label value: {value!r}")
        selector = f"{APPLICATION_LABEL}={application_id},{ENVIRONMENT_LABEL}={environment},{TRACK_LABEL}=canary"
        result = self._kubectl(["get", "ingress", "--all-namespaces", "-l", selector, "-o", "json"])
        if result.returncode != 0:
            raise RuntimeError(f"cannot list canary ingresses: {(result.stderr or result.stdout).strip()[-300:]}")
        items = (json.loads(result.stdout or "{}").get("items") or [])
        if not items:
            raise CanaryIngressMissing(
                f"no canary ingress for application {application_id} in {environment}: the canary "
                "release has not been deployed (or has been removed), so there is no weight to set"
            )
        if len(items) > 1:
            names = ", ".join(f"{i['metadata']['namespace']}/{i['metadata']['name']}" for i in items)
            raise RuntimeError(f"more than one canary ingress for application {application_id} in {environment}: {names}")
        return items[0]

    def _annotate(self, ingress: dict[str, Any], annotations: dict[str, str | None]) -> None:
        metadata = ingress["metadata"]
        patch = json.dumps({"metadata": {"annotations": annotations}})
        result = self._kubectl(
            ["patch", "ingress", metadata["name"], "-n", metadata["namespace"], "--type", "merge", "-p", patch]
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"cannot patch ingress {metadata['namespace']}/{metadata['name']}: {(result.stderr or result.stdout).strip()[-300:]}"
            )

    def _kubectl(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                [self.kubectl, "--kubeconfig", self.kubeconfig, *args],
                capture_output=True, text=True, timeout=self.timeout_seconds, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError(f"kubectl failed: {exc}") from exc


def build_nginx_ingress_router() -> NginxIngressTrafficRouter:
    kubeconfig = os.getenv("NETCI_TRAFFIC_KUBECONFIG", "").strip()
    if not kubeconfig or not Path(kubeconfig).is_file():
        raise ValueError("NETCI_TRAFFIC_ROUTER=nginx-ingress requires NETCI_TRAFFIC_KUBECONFIG to name a readable kubeconfig")
    return NginxIngressTrafficRouter(kubeconfig=kubeconfig)
