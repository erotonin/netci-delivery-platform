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


def project_dora(events: list[DoraEvent]) -> dict[str, float | int]:
    """Project event stream into current five DORA metrics per application."""
    ordered = sorted(events, key=lambda event: event.occurred_at)
    commits = {event.commit_sha: event for event in ordered if event.event_type == "commit" and event.commit_sha}
    deployments = [event for event in ordered if event.event_type == "deployment" and event.environment == "prod"]
    failed = [event for event in deployments if event.requires_intervention]
    rework = [event for event in deployments if not event.planned]
    recovery_times: list[float] = []

    for failure in failed:
        recovery = next((event for event in ordered if event.event_type == "recovery" and event.occurred_at >= failure.occurred_at), None)
        if recovery:
            recovery_times.append(_seconds(failure.occurred_at, recovery.occurred_at))

    lead_times: list[float] = []
    for deployment in deployments:
        if deployment.commit_sha and deployment.commit_sha in commits:
            lead_times.append(_seconds(commits[deployment.commit_sha].occurred_at, deployment.occurred_at))

    return {
        "change_lead_time_seconds_avg": sum(lead_times) / len(lead_times) if lead_times else 0,
        "deployment_frequency": len(deployments),
        "failed_deployment_recovery_time_seconds_avg": sum(recovery_times) / len(recovery_times) if recovery_times else 0,
        "change_fail_rate": len(failed) / len(deployments) if deployments else 0,
        "deployment_rework_rate": len(rework) / len(deployments) if deployments else 0,
    }
