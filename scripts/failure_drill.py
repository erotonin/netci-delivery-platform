from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable


@dataclass(frozen=True)
class CommandResult:
    exit_code: int
    duration_seconds: float
    output: str


@dataclass(frozen=True)
class StepEvidence:
    name: str
    attempt: int
    started_at: str
    finished_at: str
    exit_code: int
    duration_seconds: float
    output_tail: str


@dataclass
class DrillEvidence:
    scenario_id: str
    started_at: str
    status: str = "running"
    controller_failed_at: str | None = None
    detected_at: str | None = None
    rerouted_at: str | None = None
    completed_at: str | None = None
    controller_rejoined_at: str | None = None
    failure_reason: str | None = None
    steps: list[StepEvidence] = field(default_factory=list)

    def mttr_seconds(self) -> float | None:
        if not self.controller_failed_at or not self.completed_at:
            return None
        start = datetime.fromisoformat(self.controller_failed_at)
        end = datetime.fromisoformat(self.completed_at)
        return (end - start).total_seconds()


CommandRunner = Callable[[list[str], float], CommandResult]
Clock = Callable[[], datetime]


class DrillFailed(RuntimeError):
    pass


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def run_command(command: list[str], timeout: float) -> CommandResult:
    started = time.perf_counter()
    try:
        completed = subprocess.run(
            command,
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout,
        )
        output = completed.stdout + completed.stderr
        exit_code = completed.returncode
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout.decode() if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        stderr = exc.stderr.decode() if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        output = f"{stdout}{stderr}\ncommand timed out after {timeout:.1f}s"
        exit_code = 124
    return CommandResult(
        exit_code=exit_code,
        duration_seconds=time.perf_counter() - started,
        output=output,
    )


def _record_step(
    evidence: DrillEvidence,
    name: str,
    command: list[str],
    *,
    attempt: int,
    timeout: float,
    runner: CommandRunner,
    now: Clock,
) -> StepEvidence:
    started_at = now().isoformat()
    result = runner(command, timeout)
    finished_at = now().isoformat()
    step = StepEvidence(
        name=name,
        attempt=attempt,
        started_at=started_at,
        finished_at=finished_at,
        exit_code=result.exit_code,
        duration_seconds=round(result.duration_seconds, 3),
        output_tail=result.output[-4000:],
    )
    evidence.steps.append(step)
    return step


def _require_success(step: StepEvidence) -> None:
    if step.exit_code != 0:
        raise DrillFailed(f"{step.name} exited with {step.exit_code}")


def _wait_for_detection(
    evidence: DrillEvidence,
    command: list[str],
    *,
    runner: CommandRunner,
    now: Clock,
    sleeper: Callable[[float], None],
    command_timeout: float,
    detection_timeout: float,
    poll_interval: float,
) -> StepEvidence:
    deadline = time.monotonic() + detection_timeout
    attempt = 1
    while True:
        step = _record_step(
            evidence,
            "detect",
            command,
            attempt=attempt,
            timeout=command_timeout,
            runner=runner,
            now=now,
        )
        if step.exit_code == 0:
            return step
        if time.monotonic() >= deadline:
            raise DrillFailed(f"detect timed out after {attempt} attempt(s)")
        sleeper(poll_interval)
        attempt += 1


def execute_drill(
    *,
    scenario_id: str,
    commands: dict[str, list[str]],
    runner: CommandRunner = run_command,
    now: Clock = utc_now,
    sleeper: Callable[[float], None] = time.sleep,
    command_timeout: float = 300.0,
    detection_timeout: float = 60.0,
    poll_interval: float = 2.0,
) -> DrillEvidence:
    required_steps = {"stop", "detect", "reroute", "complete", "rejoin"}
    missing = required_steps - commands.keys()
    if missing:
        raise ValueError(f"missing drill commands: {', '.join(sorted(missing))}")

    evidence = DrillEvidence(scenario_id=scenario_id, started_at=now().isoformat())
    controller_stopped = False
    scenario_completed = False
    try:
        stop = _record_step(
            evidence,
            "stop",
            commands["stop"],
            attempt=1,
            timeout=command_timeout,
            runner=runner,
            now=now,
        )
        _require_success(stop)
        controller_stopped = True
        evidence.controller_failed_at = stop.finished_at

        detected = _wait_for_detection(
            evidence,
            commands["detect"],
            runner=runner,
            now=now,
            sleeper=sleeper,
            command_timeout=command_timeout,
            detection_timeout=detection_timeout,
            poll_interval=poll_interval,
        )
        evidence.detected_at = detected.finished_at

        reroute = _record_step(
            evidence,
            "reroute",
            commands["reroute"],
            attempt=1,
            timeout=command_timeout,
            runner=runner,
            now=now,
        )
        _require_success(reroute)
        evidence.rerouted_at = reroute.finished_at

        complete = _record_step(
            evidence,
            "complete",
            commands["complete"],
            attempt=1,
            timeout=command_timeout,
            runner=runner,
            now=now,
        )
        _require_success(complete)
        evidence.completed_at = complete.finished_at
        scenario_completed = True
    except DrillFailed as exc:
        evidence.failure_reason = str(exc)
    finally:
        if controller_stopped:
            rejoin = _record_step(
                evidence,
                "rejoin",
                commands["rejoin"],
                attempt=1,
                timeout=command_timeout,
                runner=runner,
                now=now,
            )
            if rejoin.exit_code == 0:
                evidence.controller_rejoined_at = rejoin.finished_at
            else:
                rejoin_failure = f"rejoin exited with {rejoin.exit_code}"
                evidence.failure_reason = (
                    f"{evidence.failure_reason}; {rejoin_failure}" if evidence.failure_reason else rejoin_failure
                )

    evidence.status = "passed" if scenario_completed and evidence.controller_rejoined_at else "failed"
    return evidence


def save(evidence: DrillEvidence, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = asdict(evidence)
    payload["schema_version"] = "1.0"
    payload["mttr_seconds"] = evidence.mttr_seconds()
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def parse_command(value: str) -> list[str]:
    command = shlex.split(value, posix=os.name != "nt")
    if not command:
        raise argparse.ArgumentTypeError("command cannot be empty")
    return command


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the netCI Jenkins controller failure drill")
    parser.add_argument("--scenario-id", default="HA-01")
    parser.add_argument("--stop-command", required=True, type=parse_command)
    parser.add_argument("--detect-command", required=True, type=parse_command)
    parser.add_argument("--reroute-command", required=True, type=parse_command)
    parser.add_argument("--completion-command", required=True, type=parse_command)
    parser.add_argument("--rejoin-command", required=True, type=parse_command)
    parser.add_argument("--command-timeout", type=float, default=300.0)
    parser.add_argument("--detection-timeout", type=float, default=60.0)
    parser.add_argument("--poll-interval", type=float, default=2.0)
    parser.add_argument("--output", type=Path, default=Path("evidence/failure-drill/ha-01.json"))
    args = parser.parse_args()

    evidence = execute_drill(
        scenario_id=args.scenario_id,
        commands={
            "stop": args.stop_command,
            "detect": args.detect_command,
            "reroute": args.reroute_command,
            "complete": args.completion_command,
            "rejoin": args.rejoin_command,
        },
        command_timeout=args.command_timeout,
        detection_timeout=args.detection_timeout,
        poll_interval=args.poll_interval,
    )
    save(evidence, args.output)
    print(json.dumps({"status": evidence.status, "evidence": str(args.output), "mttrSeconds": evidence.mttr_seconds()}))
    return 0 if evidence.status == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
