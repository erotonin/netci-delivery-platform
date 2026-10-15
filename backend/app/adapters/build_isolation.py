"""Per-project isolation for build agents.

Every build already runs in its own ephemeral pod (ADR-007). What the pods shared was
everything around them: one namespace, one service account, one set of network rules,
and no cache that survived the pod -- so isolation cost every project its warm cache.

This module gives each application its own build namespace on the build cluster:

  namespace     netci-build-<application>        labelled with the application id
  SA            jenkins-agent                     no API token mounted
  Role/Binding  the Jenkins controller's identity may manage pods *here* and nowhere else
  NetworkPolicy no ingress; egress only where a build needs to go
  PVC           netci-cache                       the project's warm cache (git mirror,
                                                  image layers, language caches)

`ensure` is idempotent and is called before every launch, so the guarantee holds for an
application created before this existed and after someone deletes the namespace by hand.
It fails closed: a build whose isolation cannot be established is not dispatched.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID

logger = logging.getLogger(__name__)

_NAME = re.compile(r"[^a-z0-9-]+")
NAMESPACE_PREFIX = "netci-build-"
CACHE_CLAIM = "netci-cache"
TRUSTED_CA = "netci-trusted-ca"
PULL_SECRET = "netci-pull"
AGENT_SERVICE_ACCOUNT = "jenkins-agent"


class BuildIsolationError(RuntimeError):
    """Isolation could not be established. The build is not dispatched."""


@dataclass(frozen=True)
class BuildIsolation:
    namespace: str
    service_account: str
    cache_claim: str

    def as_parameters(self) -> dict[str, str]:
        return {
            "NETCI_BUILD_NAMESPACE": self.namespace,
            "NETCI_BUILD_SERVICE_ACCOUNT": self.service_account,
            "NETCI_BUILD_CACHE_CLAIM": self.cache_claim,
        }


class BuildIsolationProvisioner(Protocol):
    mode: str

    def ensure(self, application_id: UUID, application_name: str) -> BuildIsolation | None: ...

    def describe(self) -> dict[str, Any]: ...


def namespace_for(application_id: UUID, application_name: str) -> str:
    """A DNS-1123 namespace name derived from the application, stable across restarts.

    The name is the readable part; the id suffix is what keeps two applications that
    truncate to the same prefix apart, and what ties the namespace to the application
    when the name is later reused.
    """

    slug = _NAME.sub("-", application_name.lower()).strip("-") or "app"
    suffix = hashlib.sha256(str(application_id).encode()).hexdigest()[:6]
    budget = 63 - len(NAMESPACE_PREFIX) - len(suffix) - 1
    return f"{NAMESPACE_PREFIX}{slug[:budget].rstrip('-')}-{suffix}"


class SharedNamespaceIsolation:
    """No per-project isolation: builds share the cloud's default namespace.

    Allowed only when chosen explicitly (NETCI_BUILD_ISOLATION=none); startup refuses an
    unset value outside local mode so that sharing is a decision, not a default.
    """

    mode = "none"

    def ensure(self, application_id: UUID, application_name: str) -> BuildIsolation | None:
        return None

    def describe(self) -> dict[str, Any]:
        return {"mode": "none", "status": "shared_namespace", "ready": True}


class KubernetesBuildIsolation:
    """Provision the per-project namespace with kubectl against the build cluster.

    kubectl rather than a client library: the worker already requires it for Helm and
    Ansible, and `apply` gives idempotency and server-side validation for free.
    """

    mode = "kubernetes"

    def __init__(
        self,
        *,
        kubeconfig: str,
        controller_service_account: str,
        cache_size: str = "2Gi",
        storage_class: str | None = None,
        registry_hosts: tuple[str, ...] = (),
        kubectl: str = "kubectl",
        timeout_seconds: float = 60.0,
        trusted_ca: str | None = None,
        pull_secret: str | None = None,
    ) -> None:
        if "/" not in controller_service_account:
            raise ValueError("controller service account must be <namespace>/<name>")
        self.kubeconfig = kubeconfig
        self.controller_namespace, self.controller_name = controller_service_account.split("/", 1)
        self.cache_size = cache_size
        self.storage_class = storage_class
        self.registry_hosts = registry_hosts
        self.kubectl = kubectl
        self.timeout_seconds = timeout_seconds
        # What a build pod needs that only the installation knows (the HA lab, ADR-096): the
        # registry's CA, public, as a ConfigMap the pod template mounts; and a pull credential for
        # the build images, as the agent account's image pull secret, so the template names none.
        # Without them a pod in a new namespace could neither pull its image nor trust the registry.
        self.trusted_ca = trusted_ca
        self.pull_secret = pull_secret

    # ---------------------------------------------------------------- manifests

    def manifests(self, application_id: UUID, application_name: str) -> list[dict[str, Any]]:
        namespace = namespace_for(application_id, application_name)
        labels = {
            "netci.io/application": str(application_id),
            "netci.io/managed-by": "netci",
            # Pod Security Admission: the pod template runs non-root, no privilege
            # escalation for jnlp, and no host mounts; rootless buildah needs the
            # setuid helpers, which `restricted` forbids, so the floor is `baseline`.
            "pod-security.kubernetes.io/enforce": "baseline",
        }
        items: list[dict[str, Any]] = [
            {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": namespace, "labels": labels}},
            {
                "apiVersion": "v1", "kind": "ServiceAccount",
                "metadata": {"name": AGENT_SERVICE_ACCOUNT, "namespace": namespace},
                "automountServiceAccountToken": False,
                **({"imagePullSecrets": [{"name": PULL_SECRET}]} if self.pull_secret else {}),
            },
            {
                "apiVersion": "rbac.authorization.k8s.io/v1", "kind": "Role",
                "metadata": {"name": "jenkins-controller", "namespace": namespace},
                "rules": [
                    {"apiGroups": [""], "resources": ["pods"], "verbs": ["get", "list", "watch", "create", "delete", "patch"]},
                    {"apiGroups": [""], "resources": ["pods/log"], "verbs": ["get", "watch"]},
                    {"apiGroups": [""], "resources": ["pods/exec"], "verbs": ["create", "get"]},
                ],
            },
            {
                "apiVersion": "rbac.authorization.k8s.io/v1", "kind": "RoleBinding",
                "metadata": {"name": "jenkins-controller", "namespace": namespace},
                "subjects": [{"kind": "ServiceAccount", "name": self.controller_name, "namespace": self.controller_namespace}],
                "roleRef": {"kind": "Role", "name": "jenkins-controller", "apiGroup": "rbac.authorization.k8s.io"},
            },
            {
                "apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy",
                "metadata": {"name": "build-pods", "namespace": namespace},
                "spec": {
                    "podSelector": {},
                    "policyTypes": ["Ingress"],
                    # No pod in another project's namespace, and nothing else in the
                    # cluster, can open a connection into a build pod. Egress stays open:
                    # a build fetches from git, the registry and netCI, and the addresses
                    # of those differ per installation; pinning them belongs to the
                    # cluster operator's baseline policy, not to a per-project one.
                    "ingress": [],
                },
            },
            {
                "apiVersion": "v1", "kind": "PersistentVolumeClaim",
                "metadata": {"name": CACHE_CLAIM, "namespace": namespace},
                "spec": {
                    "accessModes": ["ReadWriteOnce"],
                    "resources": {"requests": {"storage": self.cache_size}},
                    **({"storageClassName": self.storage_class} if self.storage_class else {}),
                },
            },
        ]
        if self.trusted_ca:
            items.append({
                "apiVersion": "v1", "kind": "ConfigMap",
                "metadata": {"name": TRUSTED_CA, "namespace": namespace},
                "data": {"ca.crt": self.trusted_ca},
            })
        return items

    def pull_secret_manifest(self, namespace: str) -> dict[str, Any]:
        import base64

        return {
            "apiVersion": "v1", "kind": "Secret", "type": "kubernetes.io/dockerconfigjson",
            "metadata": {"name": PULL_SECRET, "namespace": namespace},
            "data": {".dockerconfigjson": base64.b64encode(self.pull_secret.encode()).decode()},
        }

    # ------------------------------------------------------------------ actions

    def ensure(self, application_id: UUID, application_name: str) -> BuildIsolation:
        namespace = namespace_for(application_id, application_name)
        payload = "\n---\n".join(json.dumps(item) for item in self.manifests(application_id, application_name))
        result = self._kubectl(["apply", "-f", "-"], stdin=payload)
        if result.returncode != 0:
            raise BuildIsolationError(
                f"could not establish build isolation for {application_name} in {namespace}: "
                f"{(result.stderr or result.stdout).strip()[-600:]}"
            )
        if self.pull_secret:
            # Apart, and without kubectl's words: a refused Secret's message can quote its data.
            # Server-side: no GET first, so the account needs create and patch on Secrets, never read.
            secret = self._kubectl(["apply", "--server-side", "--field-manager=netci-build-isolation", "-f", "-"],
                                   stdin=json.dumps(self.pull_secret_manifest(namespace)))
            if secret.returncode != 0:
                raise BuildIsolationError(
                    f"could not write the build pull secret in {namespace} (kubectl exit {secret.returncode})")
        # A PVC that is stuck (no storage class, no provisioner) would leave every pod
        # Pending; the status is a fact of the cluster, so read it back rather than
        # assume the apply meant the claim is usable.
        claim = self._kubectl(["get", "pvc", CACHE_CLAIM, "-n", namespace, "-o", "json"])
        if claim.returncode != 0:
            raise BuildIsolationError(f"cache claim missing in {namespace}: {claim.stderr.strip()[-300:]}")
        phase = str((json.loads(claim.stdout or "{}").get("status") or {}).get("phase") or "")
        if phase == "Lost":
            raise BuildIsolationError(f"cache claim in {namespace} is {phase}")
        logger.info("build isolation ready for %s: namespace=%s cache=%s (%s)", application_name, namespace, CACHE_CLAIM, phase or "pending-binding")
        return BuildIsolation(namespace=namespace, service_account=AGENT_SERVICE_ACCOUNT, cache_claim=CACHE_CLAIM)

    def describe(self) -> dict[str, Any]:
        probe = self._kubectl(["auth", "can-i", "create", "namespaces"])
        allowed = probe.returncode == 0 and probe.stdout.strip() == "yes"
        return {
            "mode": "kubernetes",
            "status": "ready" if allowed else "unavailable",
            "controllerServiceAccount": f"{self.controller_namespace}/{self.controller_name}",
            "cacheSize": self.cache_size,
            "ready": allowed,
            **({} if allowed else {"error": (probe.stderr or probe.stdout).strip()[-300:] or "cannot create namespaces"}),
        }

    def _kubectl(self, args: list[str], stdin: str | None = None) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                [self.kubectl, "--kubeconfig", self.kubeconfig, *args],
                input=stdin, capture_output=True, text=True, timeout=self.timeout_seconds, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise BuildIsolationError(f"kubectl failed: {exc}") from exc


def _is_docker_config(text: str) -> bool:
    try:
        return isinstance(json.loads(text).get("auths"), dict)
    except (ValueError, AttributeError):
        return False


def _optional_file(name: str, valid, what: str) -> str | None:
    """A setting naming a file: absent is none; named but unreadable or wrong stops startup."""
    path = os.getenv(name, "").strip()
    if not path:
        return None
    try:
        text = Path(path).read_text()
    except OSError as exc:
        raise ValueError(f"{name} names {path}, which cannot be read: {exc.strerror}") from exc
    if not valid(text):
        raise ValueError(f"{name} must name {what}")
    return text


def build_isolation_provisioner() -> BuildIsolationProvisioner:
    from ..runtime_environment import is_local_runtime

    mode = os.getenv("NETCI_BUILD_ISOLATION", "").strip().lower()
    if mode == "kubernetes":
        kubeconfig = os.getenv("NETCI_BUILD_CLUSTER_KUBECONFIG", "").strip()
        if not kubeconfig or not Path(kubeconfig).is_file():
            raise ValueError("NETCI_BUILD_ISOLATION=kubernetes requires NETCI_BUILD_CLUSTER_KUBECONFIG to name a readable kubeconfig")
        return KubernetesBuildIsolation(
            kubeconfig=kubeconfig,
            controller_service_account=os.getenv("NETCI_BUILD_CONTROLLER_SERVICE_ACCOUNT", "netci-build/jenkins-controller").strip(),
            cache_size=os.getenv("NETCI_BUILD_CACHE_SIZE", "2Gi").strip() or "2Gi",
            storage_class=os.getenv("NETCI_BUILD_CACHE_STORAGE_CLASS", "").strip() or None,
            trusted_ca=_optional_file("NETCI_BUILD_TRUSTED_CA_FILE", lambda text: "BEGIN CERTIFICATE" in text,
                                      "a PEM certificate"),
            pull_secret=_optional_file("NETCI_BUILD_PULL_SECRET_FILE", _is_docker_config, "a docker config.json with auths"),
        )
    if mode == "none":
        return SharedNamespaceIsolation()
    if mode == "":
        if is_local_runtime():
            return SharedNamespaceIsolation()
        raise ValueError(
            "NETCI_BUILD_ISOLATION must be set outside local mode: 'kubernetes' for a namespace "
            "per project, or 'none' to state explicitly that builds share one namespace"
        )
    raise ValueError(f"NETCI_BUILD_ISOLATION must be kubernetes or none (got {mode!r})")
