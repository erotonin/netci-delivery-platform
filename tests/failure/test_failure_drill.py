from __future__ import annotations

from datetime import datetime, timedelta, timezone

from scripts.failure_drill import CommandResult, execute_drill


def advancing_clock():
    current = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def now() -> datetime:
        nonlocal current
        value = current
        current += timedelta(seconds=1)
        return value

    return now


def test_successful_drill_records_all_recovery_milestones():
    calls: list[str] = []

    def runner(command: list[str], timeout: float) -> CommandResult:
        calls.append(command[0])
        return CommandResult(exit_code=0, duration_seconds=0.1, output="ok")

    evidence = execute_drill(
        scenario_id="ha-01",
        commands={
            "stop": ["stop"],
            "detect": ["detect"],
            "reroute": ["reroute"],
            "complete": ["complete"],
            "rejoin": ["rejoin"],
        },
        runner=runner,
        now=advancing_clock(),
        sleeper=lambda _: None,
    )

    assert evidence.status == "passed"
    assert evidence.controller_failed_at is not None
    assert evidence.detected_at is not None
    assert evidence.rerouted_at is not None
    assert evidence.completed_at is not None
    assert evidence.controller_rejoined_at is not None
    assert evidence.mttr_seconds() == 6
    assert calls == ["stop", "detect", "reroute", "complete", "rejoin"]


def test_failed_reroute_is_fail_closed_but_still_rejoins_controller():
    calls: list[str] = []

    def runner(command: list[str], timeout: float) -> CommandResult:
        calls.append(command[0])
        code = 9 if command[0] == "reroute" else 0
        return CommandResult(exit_code=code, duration_seconds=0.1, output="failed" if code else "ok")

    evidence = execute_drill(
        scenario_id="ha-01",
        commands={
            "stop": ["stop"],
            "detect": ["detect"],
            "reroute": ["reroute"],
            "complete": ["complete"],
            "rejoin": ["rejoin"],
        },
        runner=runner,
        now=advancing_clock(),
        sleeper=lambda _: None,
    )

    assert evidence.status == "failed"
    assert evidence.failure_reason == "reroute exited with 9"
    assert evidence.completed_at is None
    assert evidence.controller_rejoined_at is not None
    assert calls == ["stop", "detect", "reroute", "rejoin"]
