from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass
class DrillEvidence:
    controller_failed_at: str
    detected_at: str | None = None
    rerouted_at: str | None = None
    completed_at: str | None = None
    controller_rejoined_at: str | None = None
    notes: list[str] | None = None

    def mark(self, field: str) -> None:
        setattr(self, field, datetime.now(timezone.utc).isoformat())

    def mttr_seconds(self) -> float | None:
        if not self.controller_failed_at or not self.completed_at:
            return None
        start = datetime.fromisoformat(self.controller_failed_at)
        end = datetime.fromisoformat(self.completed_at)
        return (end - start).total_seconds()


def save(evidence: DrillEvidence, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = asdict(evidence)
    payload["mttr_seconds"] = evidence.mttr_seconds()
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


if __name__ == "__main__":
    evidence = DrillEvidence(controller_failed_at=datetime.now(timezone.utc).isoformat(), notes=[])
    evidence.notes.append("Use this harness around real controller stop/health/reroute commands on Ubuntu.")
    time.sleep(0.1)
    save(evidence, Path("evidence/failure-drill/sample.json"))
    print("failure drill evidence skeleton written")
