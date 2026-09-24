from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from temporalio.client import Client
from temporalio.worker import Worker

from ..adapters.signature_verifier import build_signature_verifier
from ..logging import configure_logging
from ..runtime_environment import is_local_runtime
from .activities import AnsibleRuntimeRunner, DeliveryActivities, build_deployment_reporter, build_evidence_store
from .provision_and_deploy import ProvisionAndDeployWorkflow, RollbackWorkflow


async def main() -> None:
    # Without this the worker had no handler: every activity's INFO line -- which digest
    # was verified, against which key, from which commit -- was dropped, so a deployment
    # left no record on the host that ran it. The JSON formatter redacts secrets.
    configure_logging(os.getenv('NETCI_LOG_LEVEL', 'INFO'))
    address = os.getenv('TEMPORAL_ADDRESS', 'localhost:7233')
    namespace = os.getenv('TEMPORAL_NAMESPACE', 'default')
    client = await Client.connect(address, namespace=namespace)
    project_root = Path(os.getenv('NETCI_PROJECT_ROOT', '/workspace'))
    inventory = Path(os.getenv('NETCI_ANSIBLE_INVENTORY', project_root / 'deploy/ansible/inventories/local.ini'))
    runner = AnsibleRuntimeRunner(project_root=project_root, inventory=inventory)
    missing = runner.missing_collections()
    if missing:
        # Fail closed: a worker that cannot run the playbooks would take deployments off
        # the queue and fail every one of them at the deploy step.
        message = (
            "this worker cannot resolve the Ansible collections the playbooks need: "
            + ", ".join(missing)
            + " (install deploy/ansible/requirements.yml or set NETCI_ANSIBLE_COLLECTIONS_PATH)"
        )
        if not is_local_runtime():
            raise RuntimeError(message)
        logging.getLogger(__name__).warning("%s -- continuing because NETCI_ENVIRONMENT=local", message)
    activities = DeliveryActivities(
        build_evidence_store(project_root),
        runner,
        build_signature_verifier(),
        build_deployment_reporter(),
    )
    worker = Worker(
        client,
        task_queue=os.getenv('TEMPORAL_TASK_QUEUE', 'netci-delivery'),
        workflows=[ProvisionAndDeployWorkflow, RollbackWorkflow],
        activities=[
            activities.validate_artifact,
            activities.deploy,
            activities.health_check,
            activities.rollback,
            activities.report_deployment_result,
            activities.report_rollback_result,
        ],
    )
    await worker.run()


if __name__ == '__main__':
    asyncio.run(main())
