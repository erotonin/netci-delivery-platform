from datetime import datetime, timedelta, timezone

from app.projections.dora import DoraEvent, project_dora


def test_projects_current_five_dora_metrics():
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    events = [
        DoraEvent("commit", "app", "abc", None, None, t0),
        DoraEvent("deployment", "app", "abc", "d1", "prod", t0 + timedelta(minutes=10), successful=True),
        DoraEvent("commit", "app", "def", None, None, t0 + timedelta(hours=1)),
        DoraEvent("deployment", "app", "def", "d2", "prod", t0 + timedelta(hours=1, minutes=5), successful=False, requires_intervention=True),
        DoraEvent("recovery", "app", "def", "d2", "prod", t0 + timedelta(hours=1, minutes=15), successful=True),
        DoraEvent("deployment", "app", "def", "d3", "prod", t0 + timedelta(hours=2), successful=True, planned=False),
    ]
    result = project_dora(events)
    assert result["deployment_frequency"] == 3
    assert result["change_fail_rate"] == 1 / 3
    assert result["deployment_rework_rate"] == 1 / 3
    assert result["failed_deployment_recovery_time_seconds_avg"] == 600
    assert result["change_lead_time_seconds_avg"] > 0
