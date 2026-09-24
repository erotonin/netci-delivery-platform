from __future__ import annotations

import asyncio
from dataclasses import replace
import json
import os
import re
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
from urllib.parse import urlsplit, urlunsplit
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from temporalio import activity

from ..adapters.signature_verifier import (
    ArtifactIdentity,
    NullSignatureVerifier,
    SignatureVerificationError,
    SignatureVerifier,
)
from ..policy.rules import PolicyViolation, evaluate_artifact_evidence, provenance_required
from ..adapters.oci_blob import OciBlobError, fetch_blob, with_pull_host
from ..adapters.prometheus_metrics import MetricsUnavailable, UnconfiguredMetricsSource
from ..domain.verification import (
    Sample,
    VerificationConfigError,
    VerificationSpec,
    evaluate,
    parse_verification,
    render_query,
)
from .provision_and_deploy import DeliveryInput, DeliveryResult, RollbackResult


_SAFE_EVIDENCE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
# Parameters netCI hands the workflow for its own use, never for the playbook.
_CONTROL_PLANE_PARAMETERS = frozenset({"callback_token", "fencing_token"})
_WORKER_BLOB_SHA256 = "_netci_worker_blob_sha256"
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
        metrics_source=None,
    ) -> None:
        self.evidence_store = evidence_store
        self.runtime_runner = runtime_runner
        self.signature_verifier = signature_verifier or NullSignatureVerifier()
        self.deployment_reporter = deployment_reporter or UnconfiguredDeploymentReporter()
        # Where post-deploy verification reads metrics (ADR-046). Unconfigured raises on
        # every query, so a module that asks for verification fails closed without one.
        self.metrics_source = metrics_source or UnconfiguredMetricsSource()

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

        # Where the artifact came from, checked here and not only at the API: this is the
        # host that is about to run it, reading the attestation from the registry with
        # netCI's key and comparing it with the commit this deployment carries.
        artifact_ref = str(evidence.get("artifactRef") or "")
        if provenance_required() and not artifact_ref.startswith(("file://", "http://", "https://")):
            try:
                provenance = await self.signature_verifier.verify_provenance(
                    ArtifactIdentity(digest=delivery.artifact_digest,
                                     reference=str(evidence.get("artifactRef") or "") or None),
                    commit=delivery.commit_sha,
                    repository=delivery.source_repository,
                )
            except SignatureVerificationError as exc:
                raise PolicyViolation(f"artifact provenance verification failed: {exc}") from exc
            activity.logger.info("artifact %s: %s", delivery.artifact_digest, provenance)

    @activity.defn(name="verify_release")
    async def verify_release(self, delivery: DeliveryInput) -> dict[str, object]:
        """Watch the release's own metrics for the module's window, after it is healthy.

        The runtime health gate proves the process answers; it says nothing about the
        error rate once real traffic reaches it. This samples the module's queries every
        `intervalSeconds` for `windowMinutes`, stops at the first breach, and treats a
        metric that never returned data as a failure -- "we saw nothing" is not "fine".
        """

        try:
            spec = parse_verification(delivery.parameters.get("verification"))
        except VerificationConfigError as exc:
            return {"passed": False, "reason": f"verification configuration is invalid: {exc}", "samples": 0}
        if spec is None:
            return {"passed": True, "reason": "no post-deploy verification configured", "samples": 0}
        release = str(delivery.parameters.get("app_name") or delivery.release_name)
        try:
            queries = {
                name: render_query(template, release=release, environment=delivery.environment, track="stable")
                for name, template in spec.queries.items()
            }
        except VerificationConfigError as exc:
            return {"passed": False, "reason": f"cannot build the verification queries: {exc}", "samples": 0}

        samples: list[Sample] = []
        last_error = ""
        # A sample now and one every interval until the window is covered.
        planned = spec.window_minutes * 60 // spec.interval_seconds + 1
        while True:
            values: dict[str, float | None] = {}
            for name, promql in queries.items():
                try:
                    values[name] = await asyncio.to_thread(self.metrics_source.query, promql)
                except MetricsUnavailable as exc:
                    values[name] = None
                    last_error = str(exc)
            sample = Sample(at=datetime.now(timezone.utc), error_rate=values.get("errorRate"),
                            p95_latency_ms=values.get("p95LatencyMs"))
            samples.append(sample)
            _heartbeat(f"verification sample {len(samples)}")
            if _breaches(spec, sample) or len(samples) >= planned:
                break
            await asyncio.sleep(spec.interval_seconds)

        verdict = evaluate(spec, samples)
        reason = verdict.reason
        if not verdict.passed and last_error:
            reason += f" (last metrics error: {last_error})"
        activity.logger.info("post-deploy verification for %s: %s", delivery.artifact_digest, reason)
        return {"passed": verdict.passed, "reason": reason, "samples": verdict.samples}

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


def _breaches(spec: VerificationSpec, sample: Sample) -> bool:
    return (
        ("errorRate" in spec.queries and sample.error_rate is not None and sample.error_rate > spec.max_error_rate)
        or ("p95LatencyMs" in spec.queries and sample.p95_latency_ms is not None
            and sample.p95_latency_ms > spec.max_p95_latency_ms)
    )


def _heartbeat(detail: str) -> None:
    """Report progress to Temporal when running inside an activity; a no-op elsewhere."""

    try:
        from temporalio import activity

        if activity.in_activity():
            activity.heartbeat(detail)
    except Exception:  # noqa: BLE001 - heartbeating is best effort; the work itself is what matters
        pass


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
        self.heartbeat_interval_seconds = 10.0

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
        blob_sha256 = parameters.pop(_WORKER_BLOB_SHA256, None)
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
            "commit_sha": delivery.commit_sha,
            # A binary artifact's identity is its OCI manifest digest; the file the
            # playbook installs is the manifest's single layer, whose digest this worker
            # checked on the way to disk. Images are their manifest digest throughout.
            "artifact_sha256": blob_sha256 or delivery.artifact_digest.removeprefix("sha256:"),
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
        if delivery.runtime in {"docker", "systemd"} and isinstance(target_hosts, list) and target_hosts:
            command.extend(["--limit", ",".join(target_hosts)])
        command.extend(["--extra-vars", json.dumps(extra_vars, sort_keys=True)])
        return command

    def environment(self) -> dict[str, str]:
        """The Ansible environment the playbooks were written for.

        The playbooks name pinned collections (deploy/ansible/requirements.yml). Left to
        Ansible's defaults, the worker resolves whatever the invoking user has under
        ~/.ansible -- a different community.docker than the one tested, or no
        kubernetes.core at all, which is how the first Kubernetes deployment failed with
        "couldn't resolve module/action 'kubernetes.core.helm'".
        """

        env = dict(os.environ)
        collections = os.getenv("NETCI_ANSIBLE_COLLECTIONS_PATH", "").strip()
        if collections:
            env["ANSIBLE_COLLECTIONS_PATH"] = collections
        return env

    def required_collections(self) -> list[str]:
        requirements = self.project_root / "deploy" / "ansible" / "requirements.yml"
        if not requirements.is_file():
            return []
        names: list[str] = []
        for line in requirements.read_text().splitlines():
            stripped = line.strip()
            if stripped.startswith("- name:"):
                names.append(stripped.split(":", 1)[1].strip())
        return names

    def missing_collections(self) -> list[str]:
        """Which of the playbooks' collections this worker cannot resolve right now."""

        try:
            listing = subprocess.run(
                ["ansible-galaxy", "collection", "list", "--format", "json"],
                capture_output=True, text=True, timeout=60, env=self.environment(), cwd=self.project_root,
            )
            installed = {name for path in json.loads(listing.stdout or "{}").values() for name in path}
        except (OSError, ValueError, subprocess.TimeoutExpired):
            installed = set()
        return [name for name in self.required_collections() if name not in installed]

    async def _run(self, command: list[str]) -> None:
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=self.project_root,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self.environment(),
        )
        # Heartbeat while the playbook runs. Without it, a worker that dies mid-deploy is
        # noticed only at start_to_close (10 min); with it, Temporal hands the activity to
        # another worker within the heartbeat timeout (ADR-032). Playbooks are idempotent,
        # so a re-run on the surviving worker converges rather than doubles.
        communicate = asyncio.ensure_future(process.communicate())
        while True:
            done, _ = await asyncio.wait({communicate}, timeout=self.heartbeat_interval_seconds)
            if done:
                break
            _heartbeat(f"{Path(command[-2]).name if len(command) > 1 else 'runtime'} running")
        stdout, stderr = communicate.result()
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

    async def _materialized(self, delivery: DeliveryInput) -> DeliveryInput:
        """For a binary artifact, fetch it from the registry and hand the playbook a file.

        The signature was verified against the OCI reference (the manifest digest) by
        the validate_artifact activity; this fetch checks the manifest and the blob
        against their digests again on the way to disk. Docker and Kubernetes pull by
        digest themselves and need nothing here.
        """

        if delivery.runtime != "systemd":
            return delivery
        parameters = dict(delivery.parameters)
        reference = str(parameters.get("artifact_ref") or "").strip()
        if not reference or parameters.get("artifact_path"):
            return delivery
        reference = with_pull_host(reference, str(parameters.get("image_pull_host") or "") or None)
        cache = Path(os.getenv("NETCI_ARTIFACT_CACHE_DIR", str(Path(tempfile.gettempdir()) / "netci-artifacts")))
        allow_http = os.getenv("NETCI_REGISTRY_ALLOW_HTTP", "").strip().lower() in {"1", "true", "yes"}
        try:
            blob = await asyncio.to_thread(fetch_blob, reference, cache, allow_http=allow_http)
        except OciBlobError as exc:
            raise RuntimeError(f"artifact could not be fetched from the registry: {exc}") from exc
        parameters["artifact_path"] = str(blob.path)
        # Set by this worker from a manifest it verified, never by a pipeline parameter;
        # command_for reads it under this name and writes the playbook's artifact_sha256.
        parameters[_WORKER_BLOB_SHA256] = blob.sha256
        parameters.pop("artifact_url", None)
        # The release directory is named after the artifact identity, never a tag.
        parameters.setdefault("release_version", "rel-" + delivery.artifact_digest.removeprefix("sha256:")[:12])
        return replace(delivery, parameters=parameters)

    async def deploy(self, delivery: DeliveryInput) -> str:
        delivery = await self._materialized(delivery)
        await self._run(self.command_for("deploy", delivery))
        return f"deployment-{delivery.pipeline_run_id}"

    def target_address(self, host: str) -> str | None:
        """Where a named inventory host is reached: its `ansible_host`, or None when it
        is this machine (`ansible_connection=local`) or unknown."""

        # Beside ansible-playbook: the worker may run it from a virtualenv not on PATH.
        playbook_tool = Path(shutil.which(self.executable) or self.executable).resolve()
        inventory_tool = shutil.which("ansible-inventory", path=str(playbook_tool.parent)) \
            or shutil.which("ansible-inventory") or "ansible-inventory"
        try:
            listing = subprocess.run(
                [inventory_tool, "-i", str(self.inventory), "--host", host],
                capture_output=True, text=True, timeout=30, env=self.environment(), cwd=self.project_root,
            )
            variables = json.loads(listing.stdout or "{}") if listing.returncode == 0 else {}
        except (OSError, ValueError, subprocess.TimeoutExpired):
            return None
        if variables.get("ansible_connection") == "local":
            return None
        address = str(variables.get("ansible_host") or "").strip()
        return address or None

    async def health_check(self, delivery: DeliveryInput) -> bool:
        health_url = delivery.parameters.get("health_url")
        if health_url is None and delivery.parameters.get("runtime_health_verified") is True:
            # The managed Ansible playbooks fail the deploy activity unless their
            # runtime-native health gate succeeds (Helm wait/atomic or HTTP checks).
            return True
        if not isinstance(health_url, str) or not health_url.startswith(("http://", "https://")):
            return False
        # The health URL is written for the target ("127.0.0.1:<port>"). Probing it from
        # the worker reaches the worker, not the target: the first deployment to a
        # separate host was healthy on the host and reported failed here, and was rolled
        # back for it. Aim the probe at the host the playbook actually deployed to.
        hosts = delivery.parameters.get("target_hosts")
        if isinstance(hosts, list) and hosts:
            address = await asyncio.to_thread(self.target_address, str(hosts[0]))
            if address:
                parts = urlsplit(health_url)
                port = f":{parts.port}" if parts.port else ""
                health_url = urlunsplit((parts.scheme, f"{address}{port}", parts.path, parts.query, parts.fragment))

        def request() -> bool:
            try:
                with urllib.request.urlopen(health_url, timeout=5) as response:
                    return 200 <= response.status < 300
            except OSError:
                return False

        return await asyncio.to_thread(request)

    async def rollback(self, delivery: DeliveryInput) -> None:
        delivery = await self._materialized(delivery)
        await self._run(self.command_for("rollback", delivery))
