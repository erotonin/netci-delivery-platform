from __future__ import annotations

import argparse
import csv
import json
import os
import shlex
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from statistics import mean
from typing import Any


TIMING_PREFIX = "NETCI_TIMING_JSON="
PHASE_FIELDS = (
    "queue_seconds",
    "provisioning_seconds",
    "checkout_seconds",
    "cache_restore_seconds",
    "build_seconds",
    "cleanup_seconds",
    "other_seconds",
)


@dataclass
class BenchmarkSample:
    mode: str
    project: str
    run: int
    started_at: float
    finished_at: float
    total_seconds: float
    queue_seconds: float
    provisioning_seconds: float
    checkout_seconds: float
    cache_restore_seconds: float
    build_seconds: float
    cleanup_seconds: float
    other_seconds: float
    cache_hit: bool
    timing_complete: bool
    success: bool


def run_command(command: list[str]) -> tuple[int, float, str]:
    started = time.perf_counter()
    completed = subprocess.run(command, text=True, capture_output=True, check=False)
    output = completed.stdout + completed.stderr
    return completed.returncode, time.perf_counter() - started, output


def parse_timing(output: str) -> tuple[dict[str, float], bool, bool]:
    """Read the final structured timing record emitted by a benchmark command."""
    raw_record = next(
        (line.removeprefix(TIMING_PREFIX) for line in reversed(output.splitlines()) if line.startswith(TIMING_PREFIX)),
        None,
    )
    empty = {field: 0.0 for field in PHASE_FIELDS}
    if raw_record is None:
        return empty, False, False

    try:
        payload = json.loads(raw_record)
        phases = {
            "queue_seconds": float(payload["queueSeconds"]),
            "provisioning_seconds": float(payload["provisioningSeconds"]),
            "checkout_seconds": float(payload["checkoutSeconds"]),
            "cache_restore_seconds": float(payload["cacheRestoreSeconds"]),
            "build_seconds": float(payload["buildSeconds"]),
            "cleanup_seconds": float(payload["cleanupSeconds"]),
            "other_seconds": float(payload["otherSeconds"]),
        }
        if any(value < 0 for value in phases.values()):
            raise ValueError("timing values cannot be negative")
        return phases, bool(payload["cacheHit"]), True
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return empty, False, False


def run_benchmark(command: list[str], mode: str, project: str, run: int) -> BenchmarkSample:
    started_at = time.time()
    code, duration, output = run_command(command)
    finished_at = time.time()
    phases, cache_hit, timing_complete = parse_timing(output)
    return BenchmarkSample(
        mode=mode,
        project=project,
        run=run,
        started_at=started_at,
        finished_at=finished_at,
        total_seconds=duration,
        cache_hit=cache_hit,
        timing_complete=timing_complete,
        success=code == 0,
        **phases,
    )


def _average(samples: list[BenchmarkSample], field: str) -> float:
    return round(mean(float(getattr(sample, field)) for sample in samples), 3)


def _mode_report(samples: list[BenchmarkSample]) -> dict[str, float | int]:
    if not samples:
        raise ValueError("each benchmark mode requires at least one sample")
    return {
        "runs": len(samples),
        "successRate": round(sum(sample.success for sample in samples) / len(samples), 3),
        "totalSecondsAvg": _average(samples, "total_seconds"),
        "queueSecondsAvg": _average(samples, "queue_seconds"),
        "provisioningSecondsAvg": _average(samples, "provisioning_seconds"),
        "checkoutSecondsAvg": _average(samples, "checkout_seconds"),
        "cacheRestoreSecondsAvg": _average(samples, "cache_restore_seconds"),
        "buildSecondsAvg": _average(samples, "build_seconds"),
        "cleanupSecondsAvg": _average(samples, "cleanup_seconds"),
        "otherSecondsAvg": _average(samples, "other_seconds"),
    }


def _delta_percent(baseline: float, candidate: float) -> float | None:
    if baseline == 0:
        return None
    return round((candidate - baseline) / baseline * 100, 2)


def build_report(
    samples: list[BenchmarkSample],
    *,
    application_id: str,
    environment: str,
    regression_threshold: float,
) -> dict[str, Any]:
    baseline_samples = [sample for sample in samples if sample.mode == "baseline"]
    ephemeral_samples = [sample for sample in samples if sample.mode == "ephemeral"]
    isolated_samples = [sample for sample in samples if sample.mode == "isolated"]
    baseline = _mode_report(baseline_samples)
    ephemeral = _mode_report(ephemeral_samples)

    def compare(candidate: list[BenchmarkSample]) -> dict[str, Any]:
        report = _mode_report(candidate)
        # The first run of a mode is its cold start: a fresh cache has nothing to hit.
        # It is reported, but the steady-state figures exclude it so that a single
        # warm-up does not stand for the cost of every build after it.
        warm = candidate[1:] if len(candidate) > 1 else candidate
        warm_report = _mode_report(warm)
        return {
            **report,
            "warm": warm_report,
            "cacheHitRate": round(sum(s.cache_hit for s in candidate) / len(candidate), 3) if candidate else 0.0,
            "totalDeltaPercent": _delta_percent(float(baseline["totalSecondsAvg"]), float(report["totalSecondsAvg"])),
            "warmTotalDeltaPercent": _delta_percent(float(baseline["totalSecondsAvg"]), float(warm_report["totalSecondsAvg"])),
            "buildDeltaPercent": _delta_percent(float(baseline["buildSecondsAvg"]), float(report["buildSecondsAvg"])),
            "warmBuildDeltaPercent": _delta_percent(float(baseline["buildSecondsAvg"]), float(warm_report["buildSecondsAvg"])),
            "warmCheckoutDeltaPercent": _delta_percent(float(baseline["checkoutSecondsAvg"]), float(warm_report["checkoutSecondsAvg"])),
        }

    ephemeral_comparison = compare(ephemeral_samples) if ephemeral_samples else None
    isolated_comparison = compare(isolated_samples) if isolated_samples else None
    # The mode netCI actually dispatches to is the one judged: isolated when measured,
    # otherwise the plain ephemeral pod.
    judged = isolated_comparison or ephemeral_comparison or {}
    total_delta = judged.get("warmTotalDeltaPercent")

    if not all(sample.timing_complete for sample in samples):
        conclusion = "inconclusive"
    elif not all(sample.success for sample in samples):
        conclusion = "regression"
    elif total_delta is None or total_delta > regression_threshold:
        conclusion = "regression"
    else:
        conclusion = "acceptable"

    return {
        "schemaVersion": "1.1",
        "applicationId": application_id,
        "environment": environment,
        "regressionThresholdPercent": regression_threshold,
        "baseline": baseline,
        "ephemeral": ephemeral,
        "isolated": _mode_report(isolated_samples) if isolated_samples else None,
        "comparison": {
            "judgedMode": "isolated" if isolated_comparison else "ephemeral",
            "totalDeltaPercent": judged.get("totalDeltaPercent"),
            "warmTotalDeltaPercent": total_delta,
            "buildDeltaPercent": judged.get("buildDeltaPercent"),
            "warmBuildDeltaPercent": judged.get("warmBuildDeltaPercent"),
            "warmCheckoutDeltaPercent": judged.get("warmCheckoutDeltaPercent"),
            "cacheHitRate": judged.get("cacheHitRate", 0.0),
            "ephemeral": ephemeral_comparison,
            "isolated": isolated_comparison,
            "conclusion": conclusion,
        },
    }


def write_results(samples: list[BenchmarkSample], output: Path) -> None:
    if not samples:
        raise ValueError("cannot write an empty benchmark")
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(asdict(samples[0]).keys()))
        writer.writeheader()
        writer.writerows(asdict(sample) for sample in samples)


def parse_command(value: str) -> list[str]:
    command = shlex.split(value, posix=os.name != "nt")
    if not command:
        raise argparse.ArgumentTypeError("command cannot be empty")
    return command


def main() -> int:
    parser = argparse.ArgumentParser(description="Compare baseline and ephemeral Jenkins delivery timings")
    parser.add_argument("--baseline-command", required=True, type=parse_command)
    parser.add_argument("--ephemeral-command", required=True, type=parse_command)
    # The per-project pod with its cache claim (ADR-030). Optional so an installation
    # with NETCI_BUILD_ISOLATION=none still has a benchmark.
    parser.add_argument("--isolated-command", default=None, type=parse_command)
    parser.add_argument("--application-id", required=True)
    parser.add_argument("--environment", required=True)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--regression-threshold", type=float, default=20.0)
    parser.add_argument("--csv", type=Path, default=Path("evidence/benchmarks/samples.csv"))
    parser.add_argument("--report", type=Path, default=Path("evidence/benchmarks/report.json"))
    args = parser.parse_args()
    if args.runs < 1:
        parser.error("--runs must be greater than zero")

    samples: list[BenchmarkSample] = []
    for run in range(1, args.runs + 1):
        samples.append(run_benchmark(args.baseline_command, "baseline", args.application_id, run))
        samples.append(run_benchmark(args.ephemeral_command, "ephemeral", args.application_id, run))
        if args.isolated_command:
            samples.append(run_benchmark(args.isolated_command, "isolated", args.application_id, run))

    write_results(samples, args.csv)
    report = build_report(
        samples,
        application_id=args.application_id,
        environment=args.environment,
        regression_threshold=args.regression_threshold,
    )
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 1 if report["comparison"]["conclusion"] == "regression" else 0


if __name__ == "__main__":
    raise SystemExit(main())
