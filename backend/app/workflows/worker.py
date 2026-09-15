from __future__ import annotations

import asyncio
import os
from pathlib import Path

from temporalio.client import Client
from temporalio.worker import Worker

from ..adapters.signature_verifier import build_signature_verifier
from .activities import AnsibleRuntimeRunner, DeliveryActivities, build_deployment_reporter, build_evidence_store
from .provision_and_deploy import ProvisionAndDeployWorkflow, RollbackWorkflow


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
