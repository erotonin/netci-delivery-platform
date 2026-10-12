from datetime import datetime, timezone
import pytest

from app.domain.verification import (
    Sample,
    VerificationConfigError,
    VerificationResult,
    VerificationSpec,
    evaluate,
    parse_verification,
    render_query,
    verdict_for_canary,
)


@pytest.mark.parametrize(
    "raw, expected_field",
    [
        # Not a dict
        ("not a dict", "dict"),
        (123, "dict"),
        (True, "dict"),
        ([], "dict"),
        # Unknown top-level key
        ({"queries": {"errorRate": "up"}, "unexpectedKey": 123}, "unexpectedKey"),
        # queries missing or invalid
        ({"maxErrorRate": 0.05}, "queries"),
        ({"queries": "not a dict"}, "queries"),
        ({"queries": {}}, "queries"),
        ({"queries": {"unsupportedMetric": "up"}}, "queries"),
        # Query string invalid
        ({"queries": {"errorRate": ""}}, "errorRate"),
        ({"queries": {"errorRate": "   "}}, "errorRate"),
        ({"queries": {"errorRate": 123}}, "errorRate"),
        ({"queries": {"errorRate": "a" * 2001}}, "errorRate"),
        ({"queries": {"errorRate": "http_requests{app='{bad_placeholder}'}"}}, "errorRate"),
        ({"queries": {"errorRate": "http_requests{app='{other}'}"}}, "errorRate"),
        # maxErrorRate invalid (must be in (0, 1])
        ({"queries": {"errorRate": "up"}, "maxErrorRate": 0}, "maxErrorRate"),
        ({"queries": {"errorRate": "up"}, "maxErrorRate": 0.0}, "maxErrorRate"),
        ({"queries": {"errorRate": "up"}, "maxErrorRate": -0.1}, "maxErrorRate"),
        ({"queries": {"errorRate": "up"}, "maxErrorRate": 1.01}, "maxErrorRate"),
        ({"queries": {"errorRate": "up"}, "maxErrorRate": True}, "maxErrorRate"),
        ({"queries": {"errorRate": "up"}, "maxErrorRate": "0.02"}, "maxErrorRate"),
        # maxP95LatencyMs invalid (must be > 0)
        ({"queries": {"p95LatencyMs": "up"}, "maxP95LatencyMs": 0}, "maxP95LatencyMs"),
        ({"queries": {"p95LatencyMs": "up"}, "maxP95LatencyMs": -10}, "maxP95LatencyMs"),
        ({"queries": {"p95LatencyMs": "up"}, "maxP95LatencyMs": True}, "maxP95LatencyMs"),
        ({"queries": {"p95LatencyMs": "up"}, "maxP95LatencyMs": "1000"}, "maxP95LatencyMs"),
        # windowMinutes invalid (int in 1..60, bool not int)
        ({"queries": {"errorRate": "up"}, "windowMinutes": 0}, "windowMinutes"),
        ({"queries": {"errorRate": "up"}, "windowMinutes": 61}, "windowMinutes"),
        ({"queries": {"errorRate": "up"}, "windowMinutes": True}, "windowMinutes"),
        ({"queries": {"errorRate": "up"}, "windowMinutes": 5.5}, "windowMinutes"),
        ({"queries": {"errorRate": "up"}, "windowMinutes": "5"}, "windowMinutes"),
        # intervalSeconds invalid (int in 15..300, bool not int)
        ({"queries": {"errorRate": "up"}, "intervalSeconds": 14}, "intervalSeconds"),
        ({"queries": {"errorRate": "up"}, "intervalSeconds": 301}, "intervalSeconds"),
        ({"queries": {"errorRate": "up"}, "intervalSeconds": True}, "intervalSeconds"),
        ({"queries": {"errorRate": "up"}, "intervalSeconds": 30.0}, "intervalSeconds"),
        ({"queries": {"errorRate": "up"}, "intervalSeconds": "30"}, "intervalSeconds"),
        # Interval longer than window (1 min = 60s, interval = 61s)
        ({"queries": {"errorRate": "up"}, "windowMinutes": 1, "intervalSeconds": 61}, "intervalSeconds"),
        # environments invalid (non-empty list of distinct dev/staging/prod)
        ({"queries": {"errorRate": "up"}, "environments": []}, "environments"),
        ({"queries": {"errorRate": "up"}, "environments": "prod"}, "environments"),
        ({"queries": {"errorRate": "up"}, "environments": ["dev", "invalid_env"]}, "environments"),
        ({"queries": {"errorRate": "up"}, "environments": ["prod", "prod"]}, "environments"),
        ({"queries": {"errorRate": "up"}, "environments": [123]}, "environments"),
    ],
)
def test_parse_verification_refusal_matrix(raw, expected_field):
    with pytest.raises(VerificationConfigError) as exc_info:
        parse_verification(raw)
    assert expected_field.lower() in str(exc_info.value).lower()


def test_parse_verification_none():
    assert parse_verification(None) is None


def test_parse_verification_defaults():
    raw = {"queries": {"errorRate": "sum(rate(http_requests_total[1m]))"}}
    spec = parse_verification(raw)
    assert isinstance(spec, VerificationSpec)
    assert spec.queries == {"errorRate": "sum(rate(http_requests_total[1m]))"}
    assert spec.max_error_rate == 0.02
    assert spec.max_p95_latency_ms == 1000.0
    assert spec.window_minutes == 5
    assert spec.interval_seconds == 30
    assert spec.environments == ("staging", "prod")

    # Check as_json output matches expected structure
    json_shape = spec.as_json()
    assert json_shape == {
        "queries": {"errorRate": "sum(rate(http_requests_total[1m]))"},
        "maxErrorRate": 0.02,
        "maxP95LatencyMs": 1000.0,
        "windowMinutes": 5,
        "intervalSeconds": 30,
        "environments": ["staging", "prod"],
    }

    # Check applies_to
    assert spec.applies_to("staging") is True
    assert spec.applies_to("prod") is True
    assert spec.applies_to("dev") is False


def test_placeholder_whitelist():
    # Only {release}, {environment}, {track} are accepted
    valid_query = 'sum(rate(http_requests_total{app="{release}",env="{environment}",track="{track}"}[1m]))'
    spec = parse_verification({"queries": {"errorRate": valid_query}})
    assert spec is not None

    invalid_query = 'sum(rate(http_requests_total{app="{release}",cluster="{cluster}"}[1m]))'
    with pytest.raises(VerificationConfigError) as exc_info:
        parse_verification({"queries": {"errorRate": invalid_query}})
    assert "cluster" in str(exc_info.value)


@pytest.mark.parametrize(
    "bad_value",
    [
        'x"}or vector(1)',
        "ReleaseApp",
        "-app",
        "app-",
        "app_name",
        "app.name",
        "a" * 64,
        "",
    ],
)
def test_render_query_refuses_invalid_values(bad_value):
    template = 'rate(http_requests{app="{release}"}[1m])'
    with pytest.raises(VerificationConfigError):
        render_query(template, release=bad_value, environment="prod", track="canary")


def test_render_query_braces_survive_and_render_correctly():
    template = 'sum(rate(http_requests_total{app="{release}",code=~"5.."}[1m]))'
    rendered = render_query(template, release="order-service-v2", environment="staging", track="canary")
    assert rendered == 'sum(rate(http_requests_total{app="order-service-v2",code=~"5.."}[1m]))'

    multi_template = 'http_requests{app="{release}",env="{environment}",track="{track}"}'
    rendered_multi = render_query(multi_template, release="billing-v1", environment="prod", track="stable")
    assert rendered_multi == 'http_requests{app="billing-v1",env="prod",track="stable"}'


def test_evaluate_no_samples():
    spec = parse_verification({"queries": {"errorRate": "up"}})
    res = evaluate(spec, [])
    assert res == VerificationResult(
        passed=False,
        reason="no metric samples were taken",
        samples=0,
    )


def test_evaluate_all_none_for_configured_query():
    spec = parse_verification({"queries": {"errorRate": "up", "p95LatencyMs": "up"}})
    t0 = datetime(2026, 9, 24, 10, 0, 0, tzinfo=timezone.utc)
    # errorRate is all None across the window
    samples = [
        Sample(at=t0, error_rate=None, p95_latency_ms=100.0),
        Sample(at=t0, error_rate=None, p95_latency_ms=120.0),
    ]
    res = evaluate(spec, samples)
    assert res.passed is False
    assert res.reason == "errorRate: Prometheus returned no data for the whole window"
    assert res.samples == 2


def test_evaluate_breach_names_metric_value_threshold_and_time():
    spec = parse_verification({
        "queries": {"errorRate": "up", "p95LatencyMs": "up"},
        "maxErrorRate": 0.02,
        "maxP95LatencyMs": 500.0,
    })
    t_breach = datetime(2026, 9, 24, 10, 5, 0, tzinfo=timezone.utc)

    # Error rate breach
    res_error = evaluate(spec, [Sample(at=t_breach, error_rate=0.035, p95_latency_ms=100.0)])
    assert res_error.passed is False
    assert "errorRate" in res_error.reason
    assert "3.50%" in res_error.reason
    assert "2.00%" in res_error.reason
    assert t_breach.isoformat() in res_error.reason

    # Latency breach
    res_latency = evaluate(spec, [Sample(at=t_breach, error_rate=0.01, p95_latency_ms=750.36)])
    assert res_latency.passed is False
    assert "p95LatencyMs" in res_latency.reason
    assert "750.4ms" in res_latency.reason
    assert "500.0ms" in res_latency.reason
    assert t_breach.isoformat() in res_latency.reason


def test_evaluate_pass():
    spec = parse_verification({"queries": {"errorRate": "up"}})
    t1 = datetime(2026, 9, 24, 10, 0, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 9, 24, 10, 1, 0, tzinfo=timezone.utc)
    samples = [
        Sample(at=t1, error_rate=0.005, p95_latency_ms=None),
        Sample(at=t2, error_rate=0.010, p95_latency_ms=None),
    ]
    res = evaluate(spec, samples)
    assert res.passed is True
    assert res.reason == "2 sample(s) over the window within thresholds"
    assert res.samples == 2


def test_evaluate_unconfigured_query_is_ignored():
    # Only p95LatencyMs configured, errorRate is None on all samples
    spec = parse_verification({"queries": {"p95LatencyMs": "up"}, "maxP95LatencyMs": 1000.0})
    t = datetime(2026, 9, 24, 10, 0, 0, tzinfo=timezone.utc)
    samples = [Sample(at=t, error_rate=None, p95_latency_ms=250.0)]
    res = evaluate(spec, samples)
    assert res.passed is True
    assert res.samples == 1


def test_evaluate_partial_none_samples_are_fine():
    spec = parse_verification({"queries": {"errorRate": "up", "p95LatencyMs": "up"}})
    t1 = datetime(2026, 9, 24, 10, 0, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 9, 24, 10, 1, 0, tzinfo=timezone.utc)
    # Sample 1 has latency only, sample 2 has error rate only; together both queries have data
    samples = [
        Sample(at=t1, error_rate=None, p95_latency_ms=200.0),
        Sample(at=t2, error_rate=0.01, p95_latency_ms=None),
    ]
    res = evaluate(spec, samples)
    assert res.passed is True
    assert res.reason == "2 sample(s) over the window within thresholds"


def test_verdict_for_canary():
    spec = parse_verification({"queries": {"errorRate": "up", "p95LatencyMs": "up"}})

    # Canary within threshold
    res_pass = verdict_for_canary(spec, error_rate=0.01, p95=200.0)
    assert res_pass.passed is True
    assert res_pass.samples == 1
    assert "1 sample(s) over the window within thresholds" in res_pass.reason

    # Canary exceeds threshold
    res_fail = verdict_for_canary(spec, error_rate=0.05, p95=200.0)
    assert res_fail.passed is False
    assert "errorRate" in res_fail.reason

    # Canary missing data
    res_nodata = verdict_for_canary(spec, error_rate=None, p95=200.0)
    assert res_nodata.passed is False
    assert "errorRate: Prometheus returned no data for the whole window" in res_nodata.reason


def test_render_query_refuses_a_trailing_newline():
    # `re.match` with `$` accepts "api\n"; the value would reach PromQL with the newline.
    with pytest.raises(VerificationConfigError):
        render_query('up{app="{release}"}', release="api\n", environment="dev", track="stable")
