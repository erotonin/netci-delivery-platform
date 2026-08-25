from __future__ import annotations

import asyncio
import os

from temporalio.client import Client
from temporalio.worker import Worker

from .provision_and_deploy import (
    ProvisionAndDeployWorkflow,
    deploy,
    health_check,
    rollback,
    validate_artifact,
    wait_for_approval,
)


async def main() -> None:
    address = os.getenv('TEMPORAL_ADDRESS', 'localhost:7233')
    namespace = os.getenv('TEMPORAL_NAMESPACE', 'default')
    client = await Client.connect(address, namespace=namespace)
    worker = Worker(
        client,
        task_queue=os.getenv('TEMPORAL_TASK_QUEUE', 'netci-delivery'),
        workflows=[ProvisionAndDeployWorkflow],
        activities=[validate_artifact, wait_for_approval, deploy, health_check, rollback],
    )
    await worker.run()


if __name__ == '__main__':
    asyncio.run(main())
