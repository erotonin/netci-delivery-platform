from scripts.benchmark import BenchmarkSample, build_report


def sample(mode: str, total: float, build: float, *, cache_hit: bool, success: bool = True) -> BenchmarkSample:
    return BenchmarkSample(
        mode=mode,
        project="app-1",
        run=1,
        started_at=1.0,
        finished_at=1.0 + total,
        total_seconds=total,
        queue_seconds=1.0,
        provisioning_seconds=2.0,
        checkout_seconds=0.0,
        cache_restore_seconds=1.0,
        build_seconds=build,
        cleanup_seconds=1.0,
        other_seconds=0.0,
        cache_hit=cache_hit,
        timing_complete=True,
        success=success,
    )


def test_build_report_compares_measured_shared_and_ephemeral_runs():
    samples = [
        sample("baseline", 10, 5, cache_hit=False),
        sample("ephemeral", 11, 5.5, cache_hit=True),
    ]

    report = build_report(samples, application_id="app-1", environment="ubuntu-lab", regression_threshold=20)

    assert report["baseline"]["totalSecondsAvg"] == 10
    assert report["ephemeral"]["provisioningSecondsAvg"] == 2
    assert report["comparison"] == {
        "totalDeltaPercent": 10.0,
        "buildDeltaPercent": 10.0,
        "cacheHitRate": 1.0,
        "conclusion": "acceptable",
    }


def test_report_is_inconclusive_when_phase_timing_is_missing():
    incomplete = sample("ephemeral", 11, 0, cache_hit=False)
    incomplete.timing_complete = False

    report = build_report(
        [sample("baseline", 10, 5, cache_hit=False), incomplete],
        application_id="app-1",
        environment="ubuntu-lab",
        regression_threshold=20,
    )

    assert report["comparison"]["conclusion"] == "inconclusive"

