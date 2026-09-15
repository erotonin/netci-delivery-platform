"""Kubernetes Dynamic Admission Controller validating pod images against supply-chain policy."""

from __future__ import annotations

import re
from typing import Any

from .policy.engine import PolicyEngine
from .store.session import PlatformSession

SHA256_IMAGE_PATTERN = re.compile(r"^.+@(?P<digest>sha256:[0-9a-f]{64})$")


class AdmissionController:
    """Validates Kubernetes AdmissionReview requests."""

    @classmethod
    def handle_admission_review(
        cls,
        session: PlatformSession,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        req = payload.get("request") or {}
        uid = req.get("uid", "")
        namespace = req.get("namespace", "default")
        is_prod = namespace.lower() in ("production", "prod")

        obj = req.get("object") or {}
        pod_name = obj.get("metadata", {}).get("name", "unknown")
        spec = obj.get("spec") or {}

        containers = list(spec.get("containers", [])) + list(spec.get("initContainers", []))

        if not containers:
            return cls._admit(uid, "No containers to inspect")

        for c in containers:
            image = str(c.get("image", ""))
            match = SHA256_IMAGE_PATTERN.match(image)
            if not match:
                # Disallow unpinned mutable tags in production
                if is_prod:
                    return cls._deny(
                        uid,
                        f"Container '{c.get('name')}' uses mutable image tag '{image}'. "
                        "Production admission requires immutable @sha256:... image digest.",
                    )
                continue

            digest = match.group("digest")

            # Look up evidence in platform store if available
            # Check pipeline runs to find the pipeline run matching this digest
            runs = session.pipeline_runs()
            matching_run = next((r for r in runs if r.artifact_digest == digest), None)
            evidence = session.security_evidence(matching_run.id) if matching_run else None

            # Evaluate policy
            decision = PolicyEngine.evaluate_and_record_artifact(
                session,
                evidence=evidence,
                expected_digest=digest,
                require_evidence=is_prod,
                target_id=pod_name,
            )

            if not decision.allowed:
                return cls._deny(
                    uid,
                    f"Admission denied for container '{c.get('name')}' ({digest}): {decision.reason}",
                )

        return cls._admit(uid, "All container images verified against supply-chain policy")

    @staticmethod
    def _admit(uid: str, message: str) -> dict[str, Any]:
        return {
            "apiVersion": "admission.k8s.io/v1",
            "kind": "AdmissionReview",
            "response": {
                "uid": uid,
                "allowed": True,
                "status": {"code": 200, "message": message},
            },
        }

    @staticmethod
    def _deny(uid: str, message: str) -> dict[str, Any]:
        return {
            "apiVersion": "admission.k8s.io/v1",
            "kind": "AdmissionReview",
            "response": {
                "uid": uid,
                "allowed": False,
                "status": {"code": 403, "message": message},
            },
        }
