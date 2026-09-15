from __future__ import annotations

import asyncio
import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Protocol

from temporalio import activity

from ..adapters.signature_verifier import (
    ArtifactIdentity,
    NullSignatureVerifier,
    SignatureVerificationError,
    SignatureVerifier,
)
from ..policy.rules import PolicyViolation, evaluate_artifact_evidence
from .provision_and_deploy import DeliveryInput, DeliveryResult, RollbackResult


_SAFE_EVIDENCE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
# Parameters netCI hands the workflow for its own use, never for the playbook.
_CONTROL_PLANE_PARAMETERS = frozenset({"callback_token", "fencing_token"})
_SAFE_INVENTORY_HOST = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,252}$")
_SAFE_SECRET_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class EvidenceStore(Protocol):
    def load(self, pipeline_run_id: str) -> dict[str, object]: ...


class RuntimeRunner(Protocol):
    async def deploy(self, delivery: DeliveryInput) -> str: ...

    async def health_check(self, delivery: DeliveryInput) -> bool: ...

    async def rollback(self, delivery: DeliveryInput) -> None: ...


class DeploymentReporter(Protocol):
    async def report(self, result: DeliveryResult) -> None: ...

    async def report_rollback(self, result: RollbackResult) -> None: ...


class UnconfiguredDeploymentReporter:
    async def report(self, result: DeliveryResult) -> None:
        raise RuntimeError("deployment result reporting is not configured")

    async def report_rollback(self, result: RollbackResult) -> None:
        raise RuntimeError("deployment result reporting is not configured")


class HttpDeploymentReporter:
    """Close the workflow loop by writing its terminal result back to netCI."""

    def __init__(self, base_url: str, api_key: str, *, timeout_seconds: float = 15.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds

    async def report(self, result: DeliveryResult) -> None:
        if not _SAFE_EVIDENCE_ID.fullmatch(result.deployment_id):
            raise RuntimeError("invalid deployment id for result callback")
        status = "healthy" if result.status == "healthy" else "failed"
        payload: dict[str, object] = {"status": status, "message": result.message or result.status}
        if result.fencing_token is not None:
            # Without this netCI cannot tell a superseded workflow from the current one.
            payload["fencingToken"] = result.fencing_token
        body = json.dumps(payload).encode()
        # The token minted for this deployment when its workflow started. The shared key
        # is only a fallback for workflows started before per-deployment tokens existed,
        # and outside local mode netCI refuses it anyway.
        credential = result.callback_token or self.api_key
        request = urllib.request.Request(
            f"{self.base_url}/deployments/{result.deployment_id}/result",
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {credential}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )

        def send() -> None:
            try:
                with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                    if response.status not in {200, 202}:
                        raise RuntimeError(f"netCI returned {response.status} for deployment callback")
            except urllib.error.HTTPError as exc:
                raise RuntimeError(f"netCI returned {exc.code} for deployment callback") from exc
            except urllib.error.URLError as exc:
                raise RuntimeError(f"cannot report deployment result: {exc}") from exc

        await asyncio.to_thread(send)

    async def report_rollback(self, result: RollbackResult) -> None:
        if not _SAFE_EVIDENCE_ID.fullmatch(result.deployment_id):
            raise RuntimeError("invalid deployment id for rollback callback")
        payload: dict[str, object] = {"succeeded": bool(result.succeeded), "message": result.message or ""}
        if result.fencing_token is not None:
            payload["fencingToken"] = result.fencing_token
        body = json.dumps(payload).encode()
        credential = result.callback_token or self.api_key
        request = urllib.request.Request(
            f"{self.base_url}/deployments/{result.deployment_id}/rollback-result",
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {credential}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )

        def send() -> None:
            try:
                with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                    if response.status not in {200, 202}:
                        raise RuntimeError(f"netCI returned {response.status} for rollback callback")
            except urllib.error.HTTPError as exc:
                raise RuntimeError(f"netCI returned {exc.code} for rollback callback") from exc
            except urllib.error.URLError as exc:
                raise RuntimeError(f"cannot report rollback result: {exc}") from exc

        await asyncio.to_thread(send)


def build_deployment_reporter() -> DeploymentReporter:
    base_url = os.getenv("NETCI_API_URL", "").strip()
    # Per-deployment tokens arrive in the workflow input; the shared key is a fallback
    # that only local mode accepts. A worker with neither can still report, because the
    # token it needs comes with each deployment, not from its environment.
    api_key = os.getenv("NETCI_PIPELINE_API_KEY", "").strip()
    if not base_url:
        raise RuntimeError("NETCI_API_URL is required by the Temporal worker")
    return HttpDeploymentReporter(base_url, api_key)


class FileEvidenceStore:
    """Read immutable CI evidence through a local-substitutable filesystem seam."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def load(self, pipeline_run_id: str) -> dict[str, object]:
        if not _SAFE_EVIDENCE_ID.fullmatch(pipeline_run_id):
            raise PolicyViolation("invalid pipeline run id for evidence lookup")
        path = self.root / f"{pipeline_run_id}.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise PolicyViolation(f"security evidence missing for pipeline run {pipeline_run_id}") from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise PolicyViolation(f"security evidence is unreadable for pipeline run {pipeline_run_id}") from exc
        if not isinstance(payload, dict):
            raise PolicyViolation("security evidence must be a JSON object")
        return payload


class HttpEvidenceStore:
    """Read evidence from netCI itself, so CI and CD agree on one record.

    A file on a shared volume is fine for a single-host lab; a real deployment reads
    the evidence the API stored against the run, which is also what the audit trail
    references.
    """

    def __init__(self, base_url: str, api_key: str, *, timeout_seconds: float = 15.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds

    def load(self, pipeline_run_id: str, credential: str = "") -> dict[str, object]:
        if not _SAFE_EVIDENCE_ID.fullmatch(pipeline_run_id):
            raise PolicyViolation("invalid pipeline run id for evidence lookup")
        # The per-deployment token carries `ci:evidence`; the shared key is the fallback
        # that only local mode accepts.
        request = urllib.request.Request(
            f"{self.base_url}/pipeline-runs/{pipeline_run_id}/security-evidence",
            headers={"Authorization": f"Bearer {credential or self.api_key}", "Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                payload = json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise PolicyViolation(f"security evidence missing for pipeline run {pipeline_run_id}") from exc
            raise PolicyViolation(f"netCI returned {exc.code} for evidence lookup") from exc
        except (urllib.error.URLError, json.JSONDecodeError) as exc:
            raise PolicyViolation(f"cannot read security evidence: {exc}") from exc
        if not isinstance(payload, dict):
            raise PolicyViolation("security evidence must be a JSON object")
        return payload


def build_evidence_store(project_root: Path) -> EvidenceStore:
    """Prefer the API as the evidence source; fall back to the local filesystem seam."""

    base_url = os.getenv("NETCI_API_URL", "").strip()
    api_key = os.getenv("NETCI_PIPELINE_API_KEY", "").strip()
    if base_url:
        return HttpEvidenceStore(base_url, api_key)
    root = Path(os.getenv("NETCI_SECURITY_EVIDENCE_DIR", str(project_root / "evidence" / "security")))
    return FileEvidenceStore(root)


class DeliveryActivities:
    """Fail-closed delivery operations used by the Temporal worker."""

    def __init__(
        self,
        evidence_store: EvidenceStore,
        runtime_runner: RuntimeRunner,
        signature_verifier: SignatureVerifier | None = None,
        deployment_reporter: DeploymentReporter | None = None,
    ) -> None:
        self.evidence_store = evidence_store
        self.runtime_runner = runtime_runner
        self.signature_verifier = signature_verifier or NullSignatureVerifier()
        self.deployment_reporter = deployment_reporter or UnconfiguredDeploymentReporter()

    @activity.defn(name="validate_artifact")
    async def validate_artifact(self, delivery: DeliveryInput) -> None:
        """Verify the artifact before any runtime is touched.

        The decision uses the same `evaluate_artifact_evidence` rules the API applies,
        so there is exactly one definition of "deployable" in the system. Verifying
        here as well is deliberate: the workflow may run minutes or hours after CI,
        and it must not trust a decision it did not re-check.
        """

        credential = str(delivery.parameters.get("callback_token") or "")
        try:
            evidence = self.evidence_store.load(delivery.pipeline_run_id, credential)
        except TypeError:
            # File-backed stores take no credential.
            evidence = self.evidence_store.load(delivery.pipeline_run_id)
        if evidence.get("applicationId") not in (None, delivery.application_id):
            raise PolicyViolation("security evidence application does not match delivery")
        decision = evaluate_artifact_evidence(
            evidence,
            expected_digest=delivery.artifact_digest,
            require_evidence=True,
        )
        if not decision.allowed:
            raise PolicyViolation(decision.reason)

        # The policy above checked `signature.verified` -- a boolean CI wrote about its
        # own work. It records that the build believed the artifact was signed; it does
        # not prove it now. Re-verifying here uses a key netCI holds, against the digest
        # about to be deployed, on a different host than the one that produced it, so a
        # compromised or edited build cannot assert its way past the signature gate.
        signature = evidence.get("signature")
        signature = signature if isinstance(signature, dict) else {}
        try:
            outcome = await self.signature_verifier.verify(
                ArtifactIdentity(
                    digest=delivery.artifact_digest,
                    reference=str(evidence.get("artifactRef") or "") or None,
                    bundle_location=str(signature.get("bundleLocation") or "") or None,
                )
            )
        except SignatureVerificationError as exc:
            raise PolicyViolation(f"artifact signature re-verification failed: {exc}") from exc
        activity.logger.info("artifact %s: %s", delivery.artifact_digest, outcome)

    @activity.defn(name="deploy")
    async def deploy(self, delivery: DeliveryInput) -> DeliveryResult:
        runtime_deployment_id = await self.runtime_runner.deploy(delivery)
        return DeliveryResult(
            deployment_id=delivery.deployment_id or runtime_deployment_id,
            status="deploying",
            artifact_digest=delivery.artifact_digest,
        )

    @activity.defn(name="health_check")
    async def health_check(self, delivery: DeliveryInput) -> bool:
        return await self.runtime_runner.health_check(delivery)

    @activity.defn(name="rollback")
    async def rollback(self, delivery: DeliveryInput) -> None:
        await self.runtime_runner.rollback(delivery)

    @activity.defn(name="report_deployment_result")
    async def report_deployment_result(self, result: DeliveryResult) -> None:
        await self.deployment_reporter.report(result)

    @activity.defn(name="report_rollback_result")
    async def report_rollback_result(self, result: RollbackResult) -> None:
        await self.deployment_reporter.report_rollback(result)


class AnsibleRuntimeRunner:
    """Invoke the checked-in Ansible adapters without exposing command details to workflows."""

    _PLAYBOOKS = {
        "docker": "deploy-docker.yml",
        "kubernetes": "deploy-kubernetes.yml",
        "systemd": "deploy-systemd.yml",
    }

    def __init__(self, project_root: Path, inventory: Path, executable: str = "ansible-playbook") -> None:
        self.project_root = project_root.resolve()
        self.inventory = inventory.resolve()
        self.executable = executable

    def command_for(self, action: str, delivery: DeliveryInput) -> list[str]:
        try:
            playbook_name = self._PLAYBOOKS[delivery.runtime]
        except KeyError as exc:
            raise ValueError(f"unsupported runtime: {delivery.runtime}") from exc
        if action not in {"deploy", "rollback"}:
            raise ValueError(f"unsupported delivery action: {action}")
        parameters = dict(delivery.parameters)
        # Control-plane facts ride in the same parameter map as the playbook inputs. They
        # must never reach the command line: `--extra-vars` is visible to every user in
        # `ps` and is echoed by Ansible's own verbose logging, and the callback token is a
        # bearer credential that can report this deployment's result.
        for control_key in _CONTROL_PLANE_PARAMETERS:
            parameters.pop(control_key, None)
        target_hosts = parameters.pop("target_hosts", None)
        if delivery.runtime in {"docker", "systemd"}:
            if not isinstance(target_hosts, list) or not target_hosts:
                raise ValueError("docker and systemd delivery require explicit target_hosts")
            if not all(isinstance(host, str) and _SAFE_INVENTORY_HOST.fullmatch(host) for host in target_hosts):
                raise ValueError("target_hosts must contain safe inventory host names")

        # A raw kubeconfig path is never accepted from the workflow payload. Only a
        # basename-like reference resolved beneath the worker-owned secret directory is
        # allowed to become the Ansible kubeconfig argument.
        parameters.pop("kubeconfig", None)
        kubeconfig_ref = parameters.pop("kubeconfig_ref", None)
        if kubeconfig_ref is not None:
            if not isinstance(kubeconfig_ref, str) or not _SAFE_SECRET_REF.fullmatch(kubeconfig_ref):
                raise ValueError("kubeconfig_ref must be a safe secret file name")
            secret_root = Path(os.getenv("NETCI_KUBECONFIG_DIR", "/run/secrets")).resolve()
            kubeconfig = (secret_root / kubeconfig_ref).resolve()
            if not kubeconfig.is_relative_to(secret_root):
                raise ValueError("kubeconfig_ref escapes NETCI_KUBECONFIG_DIR")
            parameters["kubeconfig"] = str(kubeconfig)

        # Untrusted/configurable inputs go first. Identity, environment and immutable
        # artifact facts are written last so no pipeline parameter can replace what the
        # API and deploy-time verifier already proved.
        extra_vars: dict[str, object] = {
            **parameters,
            "netci_action": action,
            "application_id": delivery.application_id,
            "pipeline_run_id": delivery.pipeline_run_id,
            "deployment_id": delivery.deployment_id,
            "target_environment": delivery.environment,
            "artifact_digest": delivery.artifact_digest,
            "artifact_sha256": delivery.artifact_digest.removeprefix("sha256:"),
            "release_name": delivery.release_name,
        }
        playbook = self.project_root / "deploy" / "ansible" / "playbooks" / playbook_name
        command = [
            self.executable,
            "-i",
            str(self.inventory),
        ]
        private_key_file = os.getenv("NETCI_ANSIBLE_PRIVATE_KEY_FILE", "").strip()
        if private_key_file:
            secret_root = Path(os.getenv("NETCI_ANSIBLE_SECRET_DIR", "/run/secrets/netci")).resolve()
            private_key = Path(private_key_file).resolve()
            if not private_key.is_relative_to(secret_root):
                raise ValueError("NETCI_ANSIBLE_PRIVATE_KEY_FILE must stay beneath NETCI_ANSIBLE_SECRET_DIR")
            if not private_key.is_file():
                raise ValueError("NETCI_ANSIBLE_PRIVATE_KEY_FILE does not exist")
            command.extend(["--private-key", str(private_key)])
        known_hosts_file = os.getenv("NETCI_ANSIBLE_KNOWN_HOSTS_FILE", "").strip()
        if known_hosts_file:
            secret_root = Path(os.getenv("NETCI_ANSIBLE_SECRET_DIR", "/run/secrets/netci")).resolve()
            known_hosts = Path(known_hosts_file).resolve()
            if not known_hosts.is_relative_to(secret_root):
                raise ValueError("NETCI_ANSIBLE_KNOWN_HOSTS_FILE must stay beneath NETCI_ANSIBLE_SECRET_DIR")
            if not known_hosts.is_file():
                raise ValueError("NETCI_ANSIBLE_KNOWN_HOSTS_FILE does not exist")
            command.extend(
                ["--ssh-common-args", f"-o UserKnownHostsFile={known_hosts} -o StrictHostKeyChecking=yes"]
            )
        command.append(str(playbook))
        if isinstance(target_hosts, list) and target_hosts:
            command.extend(["--limit", ",".join(target_hosts)])
        command.extend(["--extra-vars", json.dumps(extra_vars, sort_keys=True)])
        return command

    async def _run(self, command: list[str]) -> None:
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=self.project_root,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        if process.returncode != 0:
            # Both streams: ansible-playbook writes the failing task to stdout and only
            # warnings to stderr, so "stderr, else stdout" reported a harmless compose
            # warning as the reason a deployment failed and hid the real one.
            detail = "\n".join(
                part.decode(errors="replace").strip()
                for part in (stdout, stderr)
                if part and part.strip()
            )[-4000:]
            raise RuntimeError(f"runtime adapter failed with exit {process.returncode}: {detail}")

    async def deploy(self, delivery: DeliveryInput) -> str:
        await self._run(self.command_for("deploy", delivery))
        return f"deployment-{delivery.pipeline_run_id}"

    async def health_check(self, delivery: DeliveryInput) -> bool:
        health_url = delivery.parameters.get("health_url")
        if health_url is None and delivery.parameters.get("runtime_health_verified") is True:
            # The managed Ansible playbooks fail the deploy activity unless their
            # runtime-native health gate succeeds (Helm wait/atomic or HTTP checks).
            return True
        if not isinstance(health_url, str) or not health_url.startswith(("http://", "https://")):
            return False

        def request() -> bool:
            try:
                with urllib.request.urlopen(health_url, timeout=5) as response:
                    return 200 <= response.status < 300
            except OSError:
                return False

        return await asyncio.to_thread(request)

    async def rollback(self, delivery: DeliveryInput) -> None:
        await self._run(self.command_for("rollback", delivery))
