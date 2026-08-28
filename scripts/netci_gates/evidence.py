"""Record what a gate actually did, so a green result is auditable.

A gate is only meaningful if you can read back the commands it ran, when it ran them,
what they returned, and which assertion each step satisfied. `EvidenceRecorder`
produces exactly that as one JSON document per gate.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
EVIDENCE_ROOT = PROJECT_ROOT / "evidence"

#: Output tails are truncated so an evidence file stays reviewable by a human.
MAX_CAPTURE = 8000


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class GateFailure(RuntimeError):
    """A gate assertion did not hold. The evidence file is still written."""


@dataclass
class Step:
    name: str
    command: list[str] | None
    started_at: datetime
    ended_at: datetime
    exit_code: int | None
    stdout: str = ""
    stderr: str = ""
    detail: dict[str, Any] = field(default_factory=dict)
    passed: bool = True

    def as_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "command": self.command,
            "startedAt": self.started_at.isoformat(),
            "endedAt": self.ended_at.isoformat(),
            "durationSeconds": round((self.ended_at - self.started_at).total_seconds(), 3),
            "exitCode": self.exit_code,
            "passed": self.passed,
            "stdout": self.stdout[-MAX_CAPTURE:],
            "stderr": self.stderr[-MAX_CAPTURE:],
            "detail": self.detail,
        }


def git_commit() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=PROJECT_ROOT, capture_output=True, text=True, check=False
        )
        return result.stdout.strip() or "unknown"
    except OSError:
        return "unknown"


class EvidenceRecorder:
    """Collect steps and assertions for one gate and write them to evidence/<gate>.json."""

    def __init__(self, gate: str, *, description: str = "") -> None:
        self.gate = gate
        self.description = description
        self.started_at = utc_now()
        self.steps: list[Step] = []
        self.assertions: list[dict[str, Any]] = []
        self.context: dict[str, Any] = {}

    # ----------------------------------------------------------------- running

    def run(
        self,
        name: str,
        command: list[str],
        *,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        expect_success: bool = True,
        timeout: float = 900.0,
        detail: dict[str, Any] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """Run a command, record it, and fail the gate if the outcome was unexpected."""

        started = utc_now()
        merged_env = {**os.environ, **(env or {})}
        try:
            completed = subprocess.run(
                command,
                cwd=str(cwd or PROJECT_ROOT),
                env=merged_env,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
            exit_code, stdout, stderr = completed.returncode, completed.stdout, completed.stderr
        except subprocess.TimeoutExpired as exc:
            exit_code = None
            stdout = (exc.stdout or b"").decode(errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            stderr = f"timed out after {timeout}s"
            completed = subprocess.CompletedProcess(command, 124, stdout, stderr)
        except FileNotFoundError as exc:
            exit_code = 127
            stdout, stderr = "", str(exc)
            completed = subprocess.CompletedProcess(command, 127, "", str(exc))

        succeeded = exit_code == 0
        passed = succeeded if expect_success else True
        step = Step(name, command, started, utc_now(), exit_code, stdout, stderr, detail or {}, passed)
        self.steps.append(step)
        if expect_success and not succeeded:
            raise GateFailure(f"{name} failed with exit {exit_code}: {(stderr or stdout)[-600:]}")
        return completed

    def record(self, name: str, detail: dict[str, Any], *, passed: bool = True) -> None:
        """Record a step that was not a subprocess (an HTTP call, a computed check)."""

        now = utc_now()
        self.steps.append(Step(name, None, now, now, 0 if passed else 1, detail=detail, passed=passed))

    # -------------------------------------------------------------- assertions

    def check(self, description: str, condition: bool, *, detail: Any = None) -> None:
        """Assert something the gate exists to prove, and keep the verdict in evidence."""

        self.assertions.append({"assertion": description, "passed": bool(condition), "detail": detail})
        if not condition:
            raise GateFailure(f"assertion failed: {description} ({detail!r})")

    def check_equal(self, description: str, actual: Any, expected: Any) -> None:
        self.check(description, actual == expected, detail={"expected": expected, "actual": actual})

    # ------------------------------------------------------------------ output

    def as_json(self, *, passed: bool, error: str | None = None) -> dict[str, Any]:
        return {
            "gate": self.gate,
            "description": self.description,
            "result": "pass" if passed else "fail",
            "error": error,
            "commit": git_commit(),
            "host": {
                "platform": platform.platform(),
                "python": platform.python_version(),
                "hostname": platform.node(),
            },
            "startedAt": self.started_at.isoformat(),
            "endedAt": utc_now().isoformat(),
            "context": self.context,
            "assertions": self.assertions,
            "steps": [step.as_json() for step in self.steps],
        }

    def write(self, *, passed: bool, error: str | None = None) -> Path:
        EVIDENCE_ROOT.mkdir(parents=True, exist_ok=True)
        target = EVIDENCE_ROOT / f"{self.gate}.json"
        target.write_text(json.dumps(self.as_json(passed=passed, error=error), indent=2), encoding="utf-8")
        return target


def run_gate(recorder: EvidenceRecorder, body) -> int:
    """Run a gate body, always write evidence, and map the outcome to an exit code."""

    try:
        body(recorder)
    except GateFailure as exc:
        target = recorder.write(passed=False, error=str(exc))
        print(f"\nGATE FAILED: {recorder.gate}\n  {exc}\n  evidence: {target}")
        return 1
    except Exception as exc:  # unexpected: still leave a record behind
        target = recorder.write(passed=False, error=f"{type(exc).__name__}: {exc}")
        print(f"\nGATE ERROR: {recorder.gate}\n  {type(exc).__name__}: {exc}\n  evidence: {target}")
        return 1
    target = recorder.write(passed=True)
    passed = sum(1 for item in recorder.assertions if item["passed"])
    print(f"\nGATE PASSED: {recorder.gate} ({passed} assertions, {len(recorder.steps)} steps)\n  evidence: {target}")
    return 0


def require_tools(recorder: EvidenceRecorder, *tools: str) -> None:
    """Refuse to run a gate whose tooling is absent, instead of silently degrading."""

    missing = [tool for tool in tools if shutil.which(tool) is None]
    recorder.record("tool-preflight", {"required": list(tools), "missing": missing}, passed=not missing)
    if missing:
        raise GateFailure(f"required tools are not installed: {', '.join(missing)}")
