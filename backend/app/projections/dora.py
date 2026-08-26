from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class DoraEvent:
    event_type: str
    application_id: str
    commit_sha: str | None
    deployment_id: str | None
    environment: str | None
    occurred_at: datetime
    successful: bool | None = None
    requires_intervention: bool = False
    planned: bool = True


def _seconds(start: datetime, end: datetime) -> float:
    return max(0.0, (end - start).total_seconds())


def project_dora(events: list[DoraEvent], application_id: str | None = None) -> dict[str, float | int]:
    """Project one application's event stream into the current five DORA metrics."""
    ordered = sorted(
        [event for event in events if application_id is None or event.application_id == application_id],
        key=lambda event: event.occurred_at,
    )
    commits = {(event.application_id, event.commit_sha): event for event in ordered if event.event_type == "commit" and event.commit_sha}
    deployments = [event for event in ordered if event.event_type == "deployment" and event.environment == "prod"]
    failed = [event for event in deployments if event.requires_intervention]
    rework = [event for event in deployments if not event.planned]
    recovery_times: list[float] = []

    for failure in failed:
        if failure.deployment_id is None:
            continue
        recovery = next(
            (
                event for event in ordered
                if event.event_type == "recovery"
                and event.application_id == failure.application_id
                and event.deployment_id == failure.deployment_id
                and event.environment == failure.environment
                and event.occurred_at >= failure.occurred_at
            ),
            None,
        )
        if recovery:
            recovery_times.append(_seconds(failure.occurred_at, recovery.occurred_at))

    lead_times: list[float] = []
    for deployment in deployments:
        commit = commits.get((deployment.application_id, deployment.commit_sha))
        if commit:
            lead_times.append(_seconds(commit.occurred_at, deployment.occurred_at))

    return {
        "change_lead_time_seconds_avg": sum(lead_times) / len(lead_times) if lead_times else 0,
        "deployment_frequency": len(deployments),
        "failed_deployment_recovery_time_seconds_avg": sum(recovery_times) / len(recovery_times) if recovery_times else 0,
        "change_fail_rate": len(failed) / len(deployments) if deployments else 0,
        "deployment_rework_rate": len(rework) / len(deployments) if deployments else 0,
    }
