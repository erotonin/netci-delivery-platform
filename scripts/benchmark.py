from __future__ import annotations

import csv
import json
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass
class BenchmarkSample:
    mode: str
    project: str
    run: int
    started_at: float
    finished_at: float
    total_seconds: float
    queue_seconds: float | None
    provisioning_seconds: float | None
    cache_restore_seconds: float | None
    build_seconds: float | None
    cleanup_seconds: float | None
    success: bool


def run_command(command: list[str]) -> tuple[int, float, str]:
    started = time.perf_counter()
    completed = subprocess.run(command, text=True, capture_output=True, check=False)
    return completed.returncode, time.perf_counter() - started, completed.stdout + completed.stderr


def run_benchmark(command: list[str], mode: str, project: str, run: int) -> BenchmarkSample:
    started_at = time.time()
    code, duration, output = run_command(command)
    finished_at = time.time()
    return BenchmarkSample(
        mode=mode,
        project=project,
        run=run,
        started_at=started_at,
        finished_at=finished_at,
        total_seconds=duration,
        queue_seconds=None,
        provisioning_seconds=None,
        cache_restore_seconds=None,
        build_seconds=None,
        cleanup_seconds=None,
        success=code == 0,
    )


def write_results(samples: list[BenchmarkSample], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(asdict(samples[0]).keys()))
        writer.writeheader()
        writer.writerows(asdict(sample) for sample in samples)


if __name__ == "__main__":
    # Runtime-specific commands are injected after Ubuntu bootstrap.
    print(json.dumps({"status": "ready", "message": "configure shared and ephemeral commands before measuring"}))
