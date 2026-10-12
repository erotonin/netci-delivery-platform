"""scripts/migrate.py retries a database that is briefly unreachable, within a deadline.

It is the API pod's init container: after a node restart the pod network needed a moment to
reach the database, one failed attempt exited, and the kubelet's back-off kept the replica
out of service for minutes.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import psycopg
import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("migrate_script", ROOT / "scripts" / "migrate.py")
migrate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(migrate)


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def test_a_database_that_comes_back_is_connected_to(monkeypatch, capsys):
    clock, calls = Clock(), []

    def flaky(url, **kwargs):
        calls.append(kwargs)
        if len(calls) < 3:
            raise psycopg.OperationalError("connection timeout expired")
        return "connection"

    monkeypatch.setattr(psycopg, "connect", flaky)
    assert migrate.connect("postgresql://u:secret@db/x", deadline_seconds=30, sleep=clock.sleep, clock=clock) == "connection"
    assert len(calls) == 3 and all(c["connect_timeout"] == 5 for c in calls)
    err = capsys.readouterr().err
    assert "retrying" in err and "secret" not in err and "postgresql://" not in err


def test_a_database_that_stays_down_still_fails_the_pod(monkeypatch):
    clock = Clock()

    def down(url, **kwargs):
        raise psycopg.OperationalError("connection refused")

    monkeypatch.setattr(psycopg, "connect", down)
    with pytest.raises(psycopg.OperationalError):
        migrate.connect("postgresql://u:p@db/x", deadline_seconds=10, sleep=clock.sleep, clock=clock)
    assert clock.now >= 10


def test_an_error_that_is_not_connectivity_is_not_retried(monkeypatch):
    def bad_url(url, **kwargs):
        raise psycopg.ProgrammingError("invalid dsn")

    monkeypatch.setattr(psycopg, "connect", bad_url)
    with pytest.raises(psycopg.ProgrammingError):
        migrate.connect("nonsense", deadline_seconds=30, sleep=lambda s: None)
