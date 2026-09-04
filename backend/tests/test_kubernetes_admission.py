"""Tests for Phase 11 Kubernetes Dynamic Admission Controller."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.admission import AdmissionController
from app.domain.models import Environment, PipelineRun, PipelineStatus
from app.main import app
from app.persistence import UnitOfWork
from app.policy.break_glass import BreakGlassService
from app.store.memory import InMemoryDatabase

DIGEST_VALID = "sha256:" + "c" * 64
DIGEST_VULNERABLE = "sha256:" + "d" * 64


def _build_admission_review_request(
    image: str,
    namespace: str = "production",
    pod_name: str = "payment-service-pod",
) -> dict:
    return {
        "apiVersion": "admission.k8s.io/v1",
        "kind": "AdmissionReview",
        "request": {
            "uid": "705ab4f5-6393-11e8-b7cc-42004e00a000",
            "namespace": namespace,
            "object": {
                "metadata": {"name": pod_name},
                "spec": {
                    "containers": [
                        {"name": "payment-service", "image": image}
                    ]
                },
            },
        },
    }


def test_admission_controller_denies_mutable_tag_in_production():
    db = InMemoryDatabase()
    review = _build_admission_review_request(
        image="registry.example.com/payment-service:latest",
        namespace="production",
    )
    with db.transaction() as session:
        response = AdmissionController.handle_admission_review(session, review)
        assert response["response"]["allowed"] is False
        assert response["response"]["status"]["code"] == 403
        assert "mutable image tag" in response["response"]["status"]["message"]


def test_admission_controller_admits_verified_sha256_image():
    db = InMemoryDatabase()
    run_id = uuid4()
    app_id = uuid4()

    with db.transaction() as session:
        # Register pipeline run with this digest
        run = PipelineRun(
            id=run_id,
            application_id=app_id,
            status=PipelineStatus.SUCCEEDED,
            commit_sha="abcdef123456",
            branch="main",
            environment=Environment.PROD,
            parameters={},
            correlation_id="c-1",
            jenkins_run_id=1,
            workflow_id=None,
            artifact_digest=DIGEST_VALID,
            started_by="dev1",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        evidence = {
            "artifactDigest": DIGEST_VALID,
            "sbom": {"generatedBy": "syft", "location": "s3://sboms/app.json"},
            "vulnerabilityScan": {"scanner": "trivy", "status": "passed", "critical": 0, "high": 0},
            "signature": {"provider": "cosign", "verified": True},
        }
        session.apply(
            UnitOfWork(
                runs=[(run, None)],
                security_evidence=[(run_id, app_id, DIGEST_VALID, evidence)],
            )
        )

        review = _build_admission_review_request(
            image=f"registry.example.com/payment-service@{DIGEST_VALID}",
            namespace="production",
        )
        response = AdmissionController.handle_admission_review(session, review)
        assert response["response"]["allowed"] is True
        assert response["response"]["status"]["code"] == 200


def test_admission_controller_denies_unwaived_vulnerability_unless_break_glass():
    db = InMemoryDatabase()
    run_id = uuid4()
    app_id = uuid4()

    with db.transaction() as session:
        run = PipelineRun(
            id=run_id,
            application_id=app_id,
            status=PipelineStatus.SUCCEEDED,
            commit_sha="abcdef123456",
            branch="main",
            environment=Environment.PROD,
            parameters={},
            correlation_id="c-2",
            jenkins_run_id=2,
            workflow_id=None,
            artifact_digest=DIGEST_VULNERABLE,
            started_by="dev1",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )

        # Vulnerable evidence
        evidence = {
            "artifactDigest": DIGEST_VULNERABLE,
            "sbom": {"generatedBy": "syft", "location": "s3://sboms/app.json"},
            "vulnerabilityScan": {
                "scanner": "trivy",
                "status": "failed",
                "critical": 2,
                "high": 1,
                "findings": ["CVE-2026-0001", "CVE-2026-0002"],
            },
            "signature": {"provider": "cosign", "verified": True},
        }
        session.apply(
            UnitOfWork(
                runs=[(run, None)],
                security_evidence=[(run_id, app_id, DIGEST_VULNERABLE, evidence)],
            )
        )

        review = _build_admission_review_request(
            image=f"registry.example.com/payment-service@{DIGEST_VULNERABLE}",
            namespace="production",
        )
        # 1. Denied initially
        denied_resp = AdmissionController.handle_admission_review(session, review)
        assert denied_resp["response"]["allowed"] is False
        assert denied_resp["response"]["status"]["code"] == 403
        assert "critical" in denied_resp["response"]["status"]["message"]

        # 2. Activate break glass for this digest
        req = BreakGlassService.create_request(
            session,
            target_type="artifact",
            target_id=DIGEST_VULNERABLE,
            requested_by="lead@corp.example",
            reason="Emergency recovery",
            incident_ticket="INC-4321",
        )
        BreakGlassService.approve_request(
            session,
            request_id=req.id,
            approved_by="vp@corp.example",
        )

        # 3. Admitted under break glass
        allowed_resp = AdmissionController.handle_admission_review(session, review)
        assert allowed_resp["response"]["allowed"] is True
        assert allowed_resp["response"]["status"]["code"] == 200


def test_admission_http_endpoint_webhook():
    client = TestClient(app)
    review = _build_admission_review_request(
        image="registry.example.com/app:v1.0.0",
        namespace="dev",  # dev allows unpinned tags
    )
    resp = client.post("/admission/validate", json=review)
    assert resp.status_code == 200
    res = resp.json()
    assert res["response"]["allowed"] is True
