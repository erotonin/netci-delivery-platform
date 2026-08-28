from __future__ import annotations

import asyncio
import os
from pathlib import Path

from temporalio.client import Client
from temporalio.worker import Worker

from ..adapters.signature_verifier import build_signature_verifier
from .activities import AnsibleRuntimeRunner, DeliveryActivities, build_evidence_store
from .provision_and_deploy import ProvisionAndDeployWorkflow


async def main() -> None:
    address = os.getenv('TEMPORAL_ADDRESS', 'localhost:7233')
    namespace = os.getenv('TEMPORAL_NAMESPACE', 'default')
    client = await Client.connect(address, namespace=namespace)
    project_root = Path(os.getenv('NETCI_PROJECT_ROOT', '/workspace'))
    inventory = Path(os.getenv('NETCI_ANSIBLE_INVENTORY', project_root / 'deploy/ansible/inventories/local.ini'))
    activities = DeliveryActivities(
        build_evidence_store(project_root),
        AnsibleRuntimeRunner(project_root=project_root, inventory=inventory),
        build_signature_verifier(),
    )
    worker = Worker(
        client,
        task_queue=os.getenv('TEMPORAL_TASK_QUEUE', 'netci-delivery'),
        workflows=[ProvisionAndDeployWorkflow],
        activities=[
            activities.validate_artifact,
            activities.deploy,
            activities.health_check,
            activities.rollback,
        ],
    )
    await worker.run()


if __name__ == '__main__':
    asyncio.run(main())
