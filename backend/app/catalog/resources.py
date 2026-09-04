"""Self-Service Resource Requests domain logic."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from ..store.records import ResourceRequestRecord
from ..store.session import PlatformSession


VALID_RESOURCE_TYPES = {
    "postgres_database",
    "redis_cache",
    "s3_bucket",
    "kafka_topic",
    "custom_domain",
}

VALID_ENVIRONMENTS = {"preview", "development", "staging", "production"}


class ResourceRequestError(ValueError):
    """Raised when self-service resource validation or processing fails."""


class SelfServiceResourceManager:
    """Manages developer self-service infrastructure requests with strict fail-closed provider contract."""

    def __init__(self, session: PlatformSession, provider: str | None = None) -> None:
        self._session = session
        # Provider configured via environment variable or explicitly passed
        self._provider = provider or os.getenv("NETCI_RESOURCE_PROVIDER", "unconfigured").lower().strip()

    def request_resource(
        self,
        *,
        application_id: UUID,
        team_id: str,
        environment: str,
        resource_type: str,
        spec: dict[str, Any] | None = None,
        requested_by: str = "developer",
    ) -> ResourceRequestRecord:
        app = self._session.application(application_id)
        if not app:
            raise ResourceRequestError(f"application '{application_id}' does not exist")

        if not team_id.strip():
            raise ResourceRequestError("team_id cannot be empty")

        if environment not in VALID_ENVIRONMENTS:
            raise ResourceRequestError(
                f"invalid environment '{environment}', must be one of {sorted(VALID_ENVIRONMENTS)}"
            )

        if resource_type not in VALID_RESOURCE_TYPES:
            raise ResourceRequestError(
                f"invalid resource_type '{resource_type}', must be one of {sorted(VALID_RESOURCE_TYPES)}"
            )

        now = datetime.now(timezone.utc)
        req_id = uuid4()
        resource_spec = dict(spec or {})

        # Determine initial status
        if environment in ("staging", "production"):
            status = "pending_approval"
            status_reason = "Requires separate team lead or platform engineer approval for non-ephemeral environment"
            outputs: dict[str, Any] = {}
        else:
            # Preview / Development environments
            if self._provider in ("terraform", "crossplane"):
                status = "ready"
                status_reason = f"Provisioned via {self._provider}"
                outputs = self._generate_outputs(resource_type, resource_spec, environment, app.name)
            else:
                status = "provider_not_configured"
                status_reason = (
                    "No infrastructure provider (Terraform/Crossplane) is configured in NETCI_RESOURCE_PROVIDER; "
                    "self-service resource provisioning failed closed"
                )
                outputs = {}

        record = ResourceRequestRecord(
            id=req_id,
            application_id=application_id,
            team_id=team_id.strip(),
            environment=environment,
            resource_type=resource_type,
            spec=resource_spec,
            status=status,
            status_reason=status_reason,
            provider=self._provider,
            outputs=outputs,
            requested_by=requested_by.strip(),
            approved_by=None,
            created_at=now,
            updated_at=now,
        )
        self._session.insert_resource_request(record)
        return record

    def approve_resource(
        self, request_id: UUID, approved_by: str, enforce_sod: bool = True
    ) -> ResourceRequestRecord:
        current = self._session.resource_request(request_id)
        if not current:
            raise ResourceRequestError(f"resource request '{request_id}' not found")

        approved_by = approved_by.strip()
        if not approved_by:
            raise ResourceRequestError("approver cannot be empty")

        if enforce_sod and current.requested_by.lower() == approved_by.lower():
            raise ResourceRequestError(
                f"Separation of duties violation: requester '{current.requested_by}' cannot approve their own resource request"
            )

        if current.status != "pending_approval":
            raise ResourceRequestError(f"cannot approve resource request in '{current.status}' status")

        app = self._session.application(current.application_id)
        app_name = app.name if app else "app"

        if self._provider in ("terraform", "crossplane"):
            new_status = "ready"
            reason = f"Approved by {approved_by} and provisioned via {self._provider}"
            outputs = self._generate_outputs(current.resource_type, current.spec, current.environment, app_name)
        else:
            new_status = "provider_not_configured"
            reason = (
                f"Approved by {approved_by}, but provider '{self._provider}' is not configured; "
                "resource provisioning failed closed"
            )
            outputs = {}

        updated = self._session.update_resource_request(
            request_id,
            status=new_status,
            status_reason=reason,
            outputs=outputs,
            approved_by=approved_by,
        )
        if not updated:
            raise ResourceRequestError(f"failed to update resource request '{request_id}'")
        return updated

    def deprovision_resource(self, request_id: UUID) -> ResourceRequestRecord:
        current = self._session.resource_request(request_id)
        if not current:
            raise ResourceRequestError(f"resource request '{request_id}' not found")

        updated = self._session.update_resource_request(
            request_id,
            status="deprovisioned",
            status_reason="Resource successfully deprovisioned",
            outputs={},
        )
        if not updated:
            raise ResourceRequestError(f"failed to deprovision resource request '{request_id}'")
        return updated

    def _generate_outputs(
        self, resource_type: str, spec: dict[str, Any], env: str, app_name: str
    ) -> dict[str, Any]:
        """Generate deterministic resource connection outputs (host, port, resource IDs) without plaintext secrets."""
        slug = f"{app_name}-{env}"
        if resource_type == "postgres_database":
            db_name = spec.get("db_name", f"{slug}_db")
            return {
                "host": f"pg-{slug}.internal.netci",
                "port": 5432,
                "database": db_name,
                "secret_reference": f"vault://secret/netci/{slug}/postgres/credentials",
            }
        elif resource_type == "redis_cache":
            return {
                "host": f"redis-{slug}.internal.netci",
                "port": 6379,
                "secret_reference": f"vault://secret/netci/{slug}/redis/credentials",
            }
        elif resource_type == "s3_bucket":
            bucket_name = spec.get("bucket_name", f"netci-{slug}-assets")
            return {
                "bucket_name": bucket_name,
                "arn": f"arn:aws:s3:::{bucket_name}",
                "region": "ap-southeast-1",
            }
        elif resource_type == "kafka_topic":
            topic_name = spec.get("topic_name", f"events.{slug}")
            return {
                "topic": topic_name,
                "partitions": spec.get("partitions", 3),
                "bootstrap_servers": "kafka.internal.netci:9092",
            }
        elif resource_type == "custom_domain":
            domain = spec.get("domain", f"{slug}.netci.internal")
            return {
                "domain": domain,
                "cname_target": "ingress.netci.internal",
                "tls_cert_status": "issued",
            }
        return {}
