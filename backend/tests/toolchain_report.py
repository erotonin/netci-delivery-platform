"""A tool report as a build on the 0.4 shared library sends it (ADR-056).

Derived from toolchain/versions.yaml, so a fixture that stands for "a good build" keeps
passing the toolchain gate when a declared version is bumped -- and a test about drift
states the drift it means.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.toolchain import declared


def declared_tool_report(**overrides: object) -> dict[str, object]:
    at = datetime.now(timezone.utc)
    tools = declared()["tools"]
    # What the callback collects (scripts/netci_callback.py collect_tool_versions).
    report: dict[str, object] = {name: tools[name]["version"] for name in ("syft", "trivy", "cosign")}
    report.update({
        "buildah": "1.39.3",
        "trivyDbUpdatedAt": (at - timedelta(hours=6)).isoformat(timespec="seconds"),
        "reportedAt": at.isoformat(timespec="seconds"),
    })
    report.update(overrides)
    return report
