"""Delivery insights: failure classes, flaky-build suspects, queue time and lead time.

A read-only projection over pipeline runs, stages and logs netCI already stores -- it
writes nothing and calls nothing external. Like the scorecard, a number this projection
cannot compute from what happened is `null`, never a guess dressed up as a metric.
"""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any, Sequence
from uuid import UUID

from ..domain.models import PipelineRun, PipelineStage, PipelineStatus

#: Substrings that mean "the platform netCI runs on broke", not "the change was bad" --
#: matched case-insensitively against log lines and stage error messages. Checked before
#: anything stage-specific, because an agent that lost its network mid-build fails
#: whatever stage was running, and blaming that stage would hide the real cause.
INFRASTRUCTURE_SIGNALS: tuple[str, ...] = (
    "no space left on device",
    "too many open files",
    "connection refused",
    "network is unreachable",
    "could not resolve host",
    "timed out",
    "timeout",
    "agent went offline",
    "pod",
    "reconciled:",
)

_RECENT_FAILURES_SHOWN = 10


def classify_failure(run: PipelineRun, stages: Sequence[PipelineStage], log_lines: Sequence[str]) -> str:
    """What kind of thing failed, in order of precedence, from what actually happened.

    `run` is accepted but not consulted -- what matters is the stage that failed and what
    was logged around it, not which run it was. Kept in the signature because a future
    signal (release_tag, trigger) may need it, and the classifier call site always has it.
    """

    del run
    texts = [line.lower() for line in log_lines if line]
    texts.extend(stage.error_message.lower() for stage in stages if stage.error_message)
    if any(signal in text for text in texts for signal in INFRASTRUCTURE_SIGNALS):
        return "infrastructure"

    failed_stage = next((stage for stage in stages if stage.status == "failed"), None)
    stage_id = failed_stage.stage_id if failed_stage else None

    if stage_id == "unit-test":
        return "tests"
    if stage_id == "build":
        return "build"
    if stage_id in {"vulnerability-scan", "sbom"} or any(
        "policy denied" in text or "artifact_policy_denied" in text for text in texts
    ):
        return "security"
    if stage_id in {"sign", "publish"} or any("provenance" in text for text in texts):
        return "supply-chain"
    if stage_id == "deploy" or any("deployment failed" in text or "rolled back" in text for text in texts):
        return "deploy"
    return "unknown"


def _nearest_rank(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, min(len(ordered), math.ceil(percentile / 100 * len(ordered))))
    return ordered[rank - 1]


def _failed_stage_id(stages: Sequence[PipelineStage]) -> str | None:
    failed_stage = next((stage for stage in stages if stage.status == "failed"), None)
    return failed_stage.stage_id if failed_stage else None


def _failures(platform: Any, failed_runs: list[PipelineRun]) -> dict[str, object]:
    by_class: dict[str, int] = defaultdict(int)
    recent: list[dict[str, object]] = []
    # Newest first, same as everything else this projection shows an operator.
    for run in sorted(failed_runs, key=lambda r: r.updated_at, reverse=True):
        stages = platform.list_pipeline_stages(run.id)
        _, log_lines = platform.get_pipeline_logs(run.id)
        failure_class = classify_failure(run, stages, log_lines)
        by_class[failure_class] += 1
        if len(recent) < _RECENT_FAILURES_SHOWN:
            recent.append({
                "pipelineRunId": str(run.id),
                "class": failure_class,
                "stage": _failed_stage_id(stages),
                "at": run.updated_at.isoformat(),
            })
    return {"total": len(failed_runs), "byClass": dict(by_class), "recent": recent}


def _flaky(runs_by_id: dict[UUID, PipelineRun], windowed: list[PipelineRun]) -> dict[str, object]:
    """Same commit, a failed run and a later succeeded one linked by `retry_of`.

    This is a *suspect*, not proof: the retry might have succeeded because the change
    truly was fixed in between commits sharing a `retry_of` chain, not because the first
    attempt was flaky. Anyone reading this number should read it that way.
    """

    commits: list[dict[str, str]] = []
    for run in windowed:
        if run.status != PipelineStatus.SUCCEEDED:
            continue
        seen: set[UUID] = set()
        node = run
        while node.retry_of is not None and node.retry_of not in seen:
            seen.add(node.retry_of)
            parent = runs_by_id.get(node.retry_of)
            if parent is None:
                break
            if parent.status == PipelineStatus.FAILED and parent.commit_sha == run.commit_sha:
                commits.append({
                    "commitSha": run.commit_sha,
                    "failedRunId": str(parent.id),
                    "passedRunId": str(run.id),
                })
                break
            node = parent
    return {"count": len(commits), "commits": commits}


def _queue_time(platform: Any, windowed: list[PipelineRun]) -> dict[str, object]:
    samples: list[float] = []
    for run in windowed:
        checkout = next(
            (stage for stage in platform.list_pipeline_stages(run.id)
             if stage.stage_id == "checkout" and stage.started_at is not None),
            None,
        )
        if checkout is not None:
            samples.append(max(0.0, (checkout.started_at - run.created_at).total_seconds()))
    return {
        "samples": len(samples),
        "p50Seconds": _nearest_rank(samples, 50),
        "p95Seconds": _nearest_rank(samples, 95),
    }


def _lead_time(windowed: list[PipelineRun], deployments) -> dict[str, object]:
    healthy_by_digest: dict[str, datetime] = {}
    for deployment in deployments:
        if deployment.healthy_at is None:
            continue
        earliest = healthy_by_digest.get(deployment.artifact_digest)
        if earliest is None or deployment.healthy_at < earliest:
            healthy_by_digest[deployment.artifact_digest] = deployment.healthy_at

    samples: list[float] = []
    for run in windowed:
        if not run.artifact_digest:
            continue
        healthy_at = healthy_by_digest.get(run.artifact_digest)
        if healthy_at is not None:
            samples.append(max(0.0, (healthy_at - run.created_at).total_seconds()))
    mean_seconds = (sum(samples) / len(samples)) if samples else None
    return {"samples": len(samples), "meanSeconds": mean_seconds}


def delivery_insights(platform: Any, application_id: UUID, *, now: datetime, days: int = 30) -> dict[str, object]:
    """Failure classes, flaky-build suspects, queue time and lead time for one application.

    `retry_of` chains and deployment lookups need runs and deployments outside the window
    too (a build that failed 40 days ago and was retried today is still the retry's
    story), so the full history is read and only the *reporting* is bounded to `days`.
    """

    all_runs = platform.list_pipeline_runs(application_id)
    runs_by_id = {run.id: run for run in all_runs}
    cutoff = now - timedelta(days=days)
    windowed = [run for run in all_runs if run.created_at >= cutoff]
    failed_runs = [run for run in windowed if run.status == PipelineStatus.FAILED]
    deployments = platform.list_deployments(application_id)

    return {
        "failures": _failures(platform, failed_runs),
        "flaky": _flaky(runs_by_id, windowed),
        "queueTime": _queue_time(platform, windowed),
        "leadTime": _lead_time(windowed, deployments),
    }
