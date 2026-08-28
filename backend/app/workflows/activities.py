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
from .provision_and_deploy import DeliveryInput, DeliveryResult


_SAFE_EVIDENCE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class EvidenceStore(Protocol):
    def load(self, pipeline_run_id: str) -> dict[str, object]: ...


class RuntimeRunner(Protocol):
    async def deploy(self, delivery: DeliveryInput) -> str: ...

    async def health_check(self, delivery: DeliveryInput) -> bool: ...

    async def rollback(self, delivery: DeliveryInput) -> None: ...


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

    def load(self, pipeline_run_id: str) -> dict[str, object]:
        if not _SAFE_EVIDENCE_ID.fullmatch(pipeline_run_id):
            raise PolicyViolation("invalid pipeline run id for evidence lookup")
        request = urllib.request.Request(
            f"{self.base_url}/pipeline-runs/{pipeline_run_id}/security-evidence",
            headers={"Authorization": f"Bearer {self.api_key}", "Accept": "application/json"},
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
    if base_url and api_key:
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
    ) -> None:
        self.evidence_store = evidence_store
        self.runtime_runner = runtime_runner
        self.signature_verifier = signature_verifier or NullSignatureVerifier()

    @activity.defn(name="validate_artifact")
    async def validate_artifact(self, delivery: DeliveryInput) -> None:
        """Verify the artifact before any runtime is touched.

        The decision uses the same `evaluate_artifact_evidence` rules the API applies,
        so there is exactly one definition of "deployable" in the system. Verifying
        here as well is deliberate: the workflow may run minutes or hours after CI,
        and it must not trust a decision it did not re-check.
        """

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
        deployment_id = await self.runtime_runner.deploy(delivery)
        return DeliveryResult(
            deployment_id=deployment_id,
            status="deploying",
            artifact_digest=delivery.artifact_digest,
        )

    @activity.defn(name="health_check")
    async def health_check(self, delivery: DeliveryInput) -> bool:
        return await self.runtime_runner.health_check(delivery)

    @activity.defn(name="rollback")
    async def rollback(self, delivery: DeliveryInput) -> None:
        await self.runtime_runner.rollback(delivery)


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
        extra_vars: dict[str, object] = {
            "netci_action": action,
            "application_id": delivery.application_id,
            "pipeline_run_id": delivery.pipeline_run_id,
            "target_environment": delivery.environment,
            "artifact_digest": delivery.artifact_digest,
            "release_name": delivery.release_name,
            **delivery.parameters,
        }
        playbook = self.project_root / "deploy" / "ansible" / "playbooks" / playbook_name
        return [
            self.executable,
            "-i",
            str(self.inventory),
            str(playbook),
            "--extra-vars",
            json.dumps(extra_vars, sort_keys=True),
        ]

    async def _run(self, command: list[str]) -> None:
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=self.project_root,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        if process.returncode != 0:
            detail = (stderr or stdout).decode(errors="replace")[-4000:]
            raise RuntimeError(f"runtime adapter failed with exit {process.returncode}: {detail}")

    async def deploy(self, delivery: DeliveryInput) -> str:
        await self._run(self.command_for("deploy", delivery))
        return f"deployment-{delivery.pipeline_run_id}"

    async def health_check(self, delivery: DeliveryInput) -> bool:
        health_url = delivery.parameters.get("health_url")
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

