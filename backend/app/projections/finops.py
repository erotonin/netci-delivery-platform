"""CI cost and supersession savings projection.

A read-only projection over pipeline runs and stages netCI already stores -- it
writes nothing and calls nothing external. Measured capacity (runner seconds and
queue seconds) and estimated capacity avoided by supersession (ADR-050) are
kept strictly distinct: every estimated field is named `estimated*` and the
response explicitly records how the estimate was computed.
"""

from __future__ import annotations

import statistics
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any
from uuid import UUID

from ..domain.models import PipelineStatus
from .insights import _nearest_rank


def _cost_dict(
    runner_seconds: float,
    estimated_avoided_seconds: float | None,
    price_per_runner_hour: Decimal | None,
    currency: str | None,
) -> dict[str, str | None] | None:
    """Compute financial cost from runner hours when a unit price is configured.

    Cost is null when no price is set -- never 0.00, which would look like free CI
    rather than unconfigured pricing.
    """
    if price_per_runner_hour is None:
        return None

    # ciSeconds / 3600 * price
    price = Decimal(str(price_per_runner_hour))
    measured_cost = (Decimal(str(runner_seconds)) * price) / Decimal(3600)
    measured_str = str(measured_cost.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

    if estimated_avoided_seconds is not None:
        avoided_cost = (Decimal(str(estimated_avoided_seconds)) * price) / Decimal(3600)
        avoided_str = str(avoided_cost.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    else:
        avoided_str = None

    return {
        "currency": currency or "USD",
        "measured": measured_str,
        "estimatedAvoided": avoided_str,
    }


def ci_cost(
    platform: Any,
    application_ids: set[UUID],
    *,
    now: datetime,
    days: int,
    price_per_runner_hour: Decimal | None,
    currency: str | None,
) -> dict[str, object]:
    """CI capacity consumed and supersession savings across visible applications.

    Pure projection over stored records; `now` is accepted explicitly so tests and
    callers control time without monkey-patching.
    """
    effective_now = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
    cutoff = effective_now - timedelta(days=days)

    application_results: list[dict[str, object]] = []
    all_queue_samples: list[float] = []

    for app_id in sorted(application_ids, key=str):
        try:
            app = platform.get_application(app_id)
            app_name = app.name
        except Exception:
            app_name = str(app_id)

        all_runs = platform.list_pipeline_runs(app_id)
        windowed_runs = []
        for run in all_runs:
            created_at = run.created_at if run.created_at.tzinfo is not None else run.created_at.replace(tzinfo=timezone.utc)
            if cutoff <= created_at <= effective_now:
                windowed_runs.append(run)

        runs_count = len(windowed_runs)
        succeeded_count = sum(1 for r in windowed_runs if r.status == PipelineStatus.SUCCEEDED)
        failed_count = sum(1 for r in windowed_runs if r.status == PipelineStatus.FAILED)
        cancelled_count = sum(1 for r in windowed_runs if r.status == PipelineStatus.CANCELLED)

        superseded_runs = [r for r in windowed_runs if r.superseded_by is not None]
        superseded_count = len(superseded_runs)
        superseded_before_admission = sum(1 for r in superseded_runs if r.admitted_at is None)
        superseded_while_building = sum(1 for r in superseded_runs if r.admitted_at is not None)

        app_stage_seconds = 0.0
        stages_without_duration = 0
        app_ci_seconds = 0.0
        runs_without_ci_timing = 0
        succeeded_run_ci_seconds: list[float] = []

        for run in windowed_runs:
            for stage in platform.list_pipeline_stages(run.id):
                if stage.duration_ms is not None:
                    app_stage_seconds += stage.duration_ms / 1000.0
                else:
                    stages_without_duration += 1
            # How long the run held CI: dispatched to Jenkins until it left CI. The stage
            # sum misses the agent starting and untimed stages, so it is not the cost.
            if run.admitted_at is None or run.status in (PipelineStatus.QUEUED, PipelineStatus.RUNNING):
                continue
            if run.ci_finished_at is None:
                runs_without_ci_timing += 1
                continue
            held = max(0.0, (run.ci_finished_at - run.admitted_at).total_seconds())
            app_ci_seconds += held
            if run.artifact_digest:
                # A build that ran to the end: it produced its artifact, whatever the
                # deployment after it did. That is what a superseded run would have cost.
                succeeded_run_ci_seconds.append(held)

        rounded_runner_seconds = round(app_ci_seconds, 1)

        if succeeded_run_ci_seconds:
            median_succeeded = float(statistics.median(succeeded_run_ci_seconds))
            estimated_avoided_runner_seconds: float | None = round(
                superseded_before_admission * median_succeeded, 1
            )
        else:
            estimated_avoided_runner_seconds = None

        app_queue_samples: list[float] = []
        for run in windowed_runs:
            if run.admitted_at is not None:
                queue_duration = max(0.0, (run.admitted_at - run.created_at).total_seconds())
                app_queue_samples.append(queue_duration)
                all_queue_samples.append(queue_duration)

        queue_seconds = round(sum(app_queue_samples), 1)
        p50_queue = _nearest_rank(app_queue_samples, 50)
        p95_queue = _nearest_rank(app_queue_samples, 95)
        if p50_queue is not None:
            p50_queue = round(p50_queue, 1)
        if p95_queue is not None:
            p95_queue = round(p95_queue, 1)

        cost = _cost_dict(
            rounded_runner_seconds,
            estimated_avoided_runner_seconds,
            price_per_runner_hour,
            currency,
        )

        application_results.append({
            "applicationId": str(app_id),
            "name": app_name,
            "runs": runs_count,
            "succeeded": succeeded_count,
            "failed": failed_count,
            "cancelled": cancelled_count,
            "superseded": superseded_count,
            "supersededBeforeAdmission": superseded_before_admission,
            "supersededWhileBuilding": superseded_while_building,
            "ciSeconds": rounded_runner_seconds,
            "runsWithoutCiTiming": runs_without_ci_timing,
            "stageSeconds": round(app_stage_seconds, 1),
            "stagesWithoutDuration": stages_without_duration,
            "queueSeconds": queue_seconds,
            "p50QueueSeconds": p50_queue,
            "p95QueueSeconds": p95_queue,
            "estimatedAvoidedRunnerSeconds": estimated_avoided_runner_seconds,
            "cost": cost,
        })

    application_results.sort(key=lambda a: (str(a["name"]), str(a["applicationId"])))

    total_runs = sum(a["runs"] for a in application_results)
    total_succeeded = sum(a["succeeded"] for a in application_results)
    total_failed = sum(a["failed"] for a in application_results)
    total_cancelled = sum(a["cancelled"] for a in application_results)
    total_superseded = sum(a["superseded"] for a in application_results)
    total_superseded_before = sum(a["supersededBeforeAdmission"] for a in application_results)
    total_superseded_while = sum(a["supersededWhileBuilding"] for a in application_results)
    total_runner_seconds = round(sum(a["ciSeconds"] for a in application_results), 1)
    total_stage_seconds = round(sum(a["stageSeconds"] for a in application_results), 1)
    total_without_timing = sum(a["runsWithoutCiTiming"] for a in application_results)
    total_stages_without_duration = sum(a["stagesWithoutDuration"] for a in application_results)
    total_queue_seconds = round(sum(a["queueSeconds"] for a in application_results), 1)

    total_p50_queue = _nearest_rank(all_queue_samples, 50)
    total_p95_queue = _nearest_rank(all_queue_samples, 95)
    if total_p50_queue is not None:
        total_p50_queue = round(total_p50_queue, 1)
    if total_p95_queue is not None:
        total_p95_queue = round(total_p95_queue, 1)

    non_null_estimates = [
        a["estimatedAvoidedRunnerSeconds"]
        for a in application_results
        if a["estimatedAvoidedRunnerSeconds"] is not None
    ]
    if non_null_estimates:
        total_estimated_avoided: float | None = round(sum(non_null_estimates), 1)
    else:
        total_estimated_avoided = None
    # An application with superseded runs but no succeeded run to estimate from adds
    # nothing to the total. Say so, or a partial sum reads as the whole saving.
    estimate_incomplete = any(
        a["estimatedAvoidedRunnerSeconds"] is None and a["supersededBeforeAdmission"]
        for a in application_results
    )

    total_cost = _cost_dict(
        total_runner_seconds,
        total_estimated_avoided,
        price_per_runner_hour,
        currency,
    )

    total_dict = {
        "runs": total_runs,
        "succeeded": total_succeeded,
        "failed": total_failed,
        "cancelled": total_cancelled,
        "superseded": total_superseded,
        "supersededBeforeAdmission": total_superseded_before,
        "supersededWhileBuilding": total_superseded_while,
        "ciSeconds": total_runner_seconds,
        "runsWithoutCiTiming": total_without_timing,
        "stageSeconds": total_stage_seconds,
        "stagesWithoutDuration": total_stages_without_duration,
        "queueSeconds": total_queue_seconds,
        "p50QueueSeconds": total_p50_queue,
        "p95QueueSeconds": total_p95_queue,
        "estimatedAvoidedRunnerSeconds": total_estimated_avoided,
        "estimatedAvoidedIncomplete": estimate_incomplete,
        "cost": total_cost,
    }

    return {
        "window": {
            "days": days,
            "from": cutoff.isoformat(),
            "to": effective_now.isoformat(),
        },
        "method": {
            "ciSeconds": "dispatch to Jenkins until the run left CI, summed over finished admitted runs",
            "stageSeconds": "sum of recorded stage durations (misses agent start-up and untimed stages)",
            "estimatedAvoidedRunnerSeconds": (
                "superseded-before-admission runs x median ciSeconds of the application's "
                "runs in the window that built their artifact"
            ),
        },
        "applications": application_results,
        "total": total_dict,
    }
