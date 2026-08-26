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


def test_recovery_without_a_deployment_identity_is_not_guessed():
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    events = [
        DoraEvent("deployment", "app-a", "abc", None, "prod", t0, successful=False, requires_intervention=True),
        DoraEvent("recovery", "app-a", "abc", None, "prod", t0 + timedelta(seconds=10), successful=True),
    ]

    result = project_dora(events)

    assert result["change_fail_rate"] == 1
    assert result["failed_deployment_recovery_time_seconds_avg"] == 0


def test_recovery_matches_application_deployment_and_environment_together():
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    events = [
        DoraEvent("deployment", "app-a", "abc", "deployment-1", "prod", t0, successful=False, requires_intervention=True),
        DoraEvent("recovery", "app-b", "abc", "deployment-1", "prod", t0 + timedelta(seconds=10), successful=True),
        DoraEvent("recovery", "app-a", "abc", "deployment-2", "prod", t0 + timedelta(seconds=20), successful=True),
        DoraEvent("recovery", "app-a", "abc", "deployment-1", "staging", t0 + timedelta(seconds=30), successful=True),
        DoraEvent("recovery", "app-a", "abc", "deployment-1", "prod", t0 + timedelta(seconds=60), successful=True),
    ]

    result = project_dora(events)

    assert result["failed_deployment_recovery_time_seconds_avg"] == 60


def test_same_commit_sha_in_another_application_does_not_change_lead_time():
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    events = [
        DoraEvent("commit", "app-a", "shared-sha", None, None, t0),
        DoraEvent("commit", "app-b", "shared-sha", None, None, t0 + timedelta(seconds=90)),
        DoraEvent("deployment", "app-a", "shared-sha", "deployment-a", "prod", t0 + timedelta(seconds=120), successful=True),
    ]

    result = project_dora(events, application_id="app-a")

    assert result["deployment_frequency"] == 1
    assert result["change_lead_time_seconds_avg"] == 120
