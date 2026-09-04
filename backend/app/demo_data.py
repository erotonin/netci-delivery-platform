"""Reference records for an explicitly opted-in local demo.

This is not a runtime fallback and it is not a fixture the API can reach on its own.
`NETCI_DEMO_DATA` must be set, and the composition root calls `seed_demo_data` once at
start-up. With the flag unset -- the default, including every production build -- a fresh
installation is empty, and the Portal shows empty states rather than invented systems.

Seeding writes real rows through the normal commands, so the demo installation exercises
the same code path an operator does. It is idempotent: a second call finds the systems
already present and does nothing.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

from .domain.models import Environment, Runtime

logger = logging.getLogger(__name__)

DEMO_APPLICATIONS: tuple[tuple[str, str, str, Runtime, str, str], ...] = (
    ("hello-container", "Hello Container", "Local container delivery application",
     Runtime.DOCKER, "container-ci-cd-v1", "Backend"),
    ("hello-kubernetes", "Hello Kubernetes", "Local Kubernetes deployment application",
     Runtime.KUBERNETES, "kubernetes-ci-cd-v1", "Workload"),
    ("hello-systemd-go", "Hello Systemd Go", "Local systemd service application",
     Runtime.SYSTEMD, "systemd-ansible-ci-cd-v1", "Backend"),
)

DEMO_VERSIONS = ("v1.2.0", "v1.1.0", "v1.0.0")


def demo_data_enabled() -> bool:
    return os.getenv("NETCI_DEMO_DATA", "false").strip().lower() in {"1", "true", "yes"}


def _environments(module_id: str, runtime: Runtime) -> list[dict[str, object]]:
    return [
        {
            "displayName": "Development",
            "environment": "dev",
            "runtime": runtime.value,
            "servers": ["localhost"],
            "tasks": ["Health check"],
        },
        {
            "displayName": "Staging",
            "environment": "staging",
            "runtime": runtime.value,
            "servers": [f"srv-{module_id}-staging"],
            "tasks": ["Health check", "Smoke tests"],
        },
        {
            "displayName": "Production",
            "environment": "prod",
            "runtime": runtime.value,
            "servers": [f"srv-{module_id}-prod"],
            "tasks": ["Health check", "Traffic shift"],
        },
    ]


def seed_demo_data(platform, portal) -> bool:
    """Write the reference systems and modules. Returns whether anything was written."""

    if not demo_data_enabled():
        return False
    written = False
    for module_id, display_name, description, runtime, template, module_type in DEMO_APPLICATIONS:
        with platform.database.transaction() as transaction:
            if transaction.portal_system(module_id) is not None:
                continue
            transaction.insert_portal_system(
                _system_row(module_id, description)
            )
            application = platform.create_application(
                name=module_id,
                repository_url=f"https://github.com/example/{module_id}",
                pipeline_template=template,
                runtime=runtime,
                default_environment=Environment.DEV,
                stages=[],
                idempotency_key=f"portal-app-{module_id}",
                session=transaction,
            )
            portal.attach_module(
                system_id=module_id,
                module_id=module_id,
                name=display_name,
                module_type=module_type,
                description=description,
                runtime=runtime,
                application_id=application.id,
                deployment_environments=_environments(module_id, runtime),
                pipeline_config={"runner": "local", "strategy": "Trunk-based"},
                session=transaction,
            )
            for tag in DEMO_VERSIONS:
                transaction.upsert_portal_version(
                    _version_row(module_id, tag)
                )
            written = True
    if written:
        logger.warning(
            "NETCI_DEMO_DATA is set: reference systems were written. "
            "Do not enable this outside a local demo."
        )
    return written


def _system_row(module_id: str, description: str):
    from .store import SystemRow

    return SystemRow(
        id=module_id,
        unit="Local Infrastructure",
        description=description,
        owner="Admin",
        status="unknown",
    )


def _version_row(module_id: str, tag: str):
    from .store import VersionRow

    return VersionRow(
        module_id=module_id,
        version=tag,
        metadata={
            "gitTagUrl": f"https://github.com/example/{module_id}/releases/tag/{tag}",
            "artifactUrl": f"http://127.0.0.1:55000/{module_id}:{tag}",
            "createdBy": "netCI Pipeline",
            "createdAt": datetime.now(timezone.utc).isoformat(),
            "ciReport": {
                "testPassCount": 42,
                "testFailCount": 0,
                "coveragePercentage": 94.5,
                "vulnerabilityScan": "passed",
                "sastPassed": True,
                "buildDurationSeconds": 48,
            },
        },
    )
