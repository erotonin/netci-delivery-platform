from urllib.error import HTTPError, URLError
import urllib.parse
import urllib.request
import pytest

from app.adapters.prometheus_metrics import (
    MetricsUnavailable,
    PrometheusMetricsSource,
    UnconfiguredMetricsSource,
    build_metrics_source,
)


class FakeResponse:
    def __init__(self, data: bytes, status: int = 200) -> None:
        self._data = data
        self.status = status

    def read(self) -> bytes:
        return self._data

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        pass


def test_query_vector_one_value(monkeypatch):
    body = (
        b'{"status": "success", "data": {"resultType": "vector", "result": '
        b'[{"metric": {}, "value": [1600000000, "0.015"]}]}}'
    )
    monkeypatch.setattr(urllib.request, "urlopen", lambda url, timeout=10: FakeResponse(body))
    source = PrometheusMetricsSource("http://prometheus:9090")
    val = source.query("sum(rate(http_requests_total[1m]))")
    assert val == 0.015


def test_query_empty_vector_returns_none(monkeypatch):
    body = b'{"status": "success", "data": {"resultType": "vector", "result": []}}'
    monkeypatch.setattr(urllib.request, "urlopen", lambda url, timeout=10: FakeResponse(body))
    source = PrometheusMetricsSource("http://prometheus:9090")
    val = source.query("sum(rate(http_requests_total[1m]))")
    assert val is None


def test_query_vector_two_series_raises_unavailable(monkeypatch):
    body = (
        b'{"status": "success", "data": {"resultType": "vector", "result": ['
        b'{"metric": {"instance": "a"}, "value": [1600000000, "0.01"]},'
        b'{"metric": {"instance": "b"}, "value": [1600000000, "0.02"]}'
        b']}}'
    )
    monkeypatch.setattr(urllib.request, "urlopen", lambda url, timeout=10: FakeResponse(body))
    source = PrometheusMetricsSource("http://prometheus:9090")
    with pytest.raises(MetricsUnavailable) as exc:
        source.query("rate(http_requests_total[1m])")
    assert "query returned 2 series; a verification query must reduce to one value (use sum/max)" in str(exc.value)


def test_query_scalar(monkeypatch):
    body = b'{"status": "success", "data": {"resultType": "scalar", "result": [1600000000, "42.5"]}}'
    monkeypatch.setattr(urllib.request, "urlopen", lambda url, timeout=10: FakeResponse(body))
    source = PrometheusMetricsSource("http://prometheus:9090")
    val = source.query("scalar(count(up))")
    assert val == 42.5


def test_query_nan_returns_none(monkeypatch):
    body = (
        b'{"status": "success", "data": {"resultType": "vector", "result": '
        b'[{"metric": {}, "value": [1600000000, "NaN"]}]}}'
    )
    monkeypatch.setattr(urllib.request, "urlopen", lambda url, timeout=10: FakeResponse(body))
    source = PrometheusMetricsSource("http://prometheus:9090")
    assert source.query("rate(http_requests_total[1m])") is None


def test_query_inf_values(monkeypatch):
    body_pos = b'{"status": "success", "data": {"resultType": "scalar", "result": [1600000000, "+Inf"]}}'
    monkeypatch.setattr(urllib.request, "urlopen", lambda url, timeout=10: FakeResponse(body_pos))
    source = PrometheusMetricsSource("http://prometheus:9090")
    assert source.query("test") == float("inf")

    body_neg = b'{"status": "success", "data": {"resultType": "scalar", "result": [1600000000, "-Inf"]}}'
    monkeypatch.setattr(urllib.request, "urlopen", lambda url, timeout=10: FakeResponse(body_neg))
    assert source.query("test") == float("-inf")


def test_query_status_error_raises_unavailable(monkeypatch):
    body = b'{"status": "error", "errorType": "bad_data", "error": "parse error: unexpected identifier"}'
    monkeypatch.setattr(urllib.request, "urlopen", lambda url, timeout=10: FakeResponse(body))
    source = PrometheusMetricsSource("http://prometheus:9090")
    with pytest.raises(MetricsUnavailable) as exc:
        source.query("bad_syntax")
    assert "parse error" in str(exc.value)


def test_query_http_error_raises_unavailable(monkeypatch):
    def mock_urlopen(url, timeout=10):
        raise HTTPError(url="http://prometheus:9090", code=503, msg="Service Unavailable", hdrs={}, fp=None)

    monkeypatch.setattr(urllib.request, "urlopen", mock_urlopen)
    source = PrometheusMetricsSource("http://prometheus:9090")
    with pytest.raises(MetricsUnavailable) as exc:
        source.query("up")
    assert "HTTP 503" in str(exc.value)


def test_query_url_error_raises_unavailable(monkeypatch):
    def mock_urlopen(url, timeout=10):
        raise URLError("Connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", mock_urlopen)
    source = PrometheusMetricsSource("http://prometheus:9090")
    with pytest.raises(MetricsUnavailable) as exc:
        source.query("up")
    assert "Connection refused" in str(exc.value)


def test_query_invalid_json_raises_unavailable(monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen", lambda url, timeout=10: FakeResponse(b"<html>Bad Gateway</html>"))
    source = PrometheusMetricsSource("http://prometheus:9090")
    with pytest.raises(MetricsUnavailable) as exc:
        source.query("up")
    assert "not valid JSON" in str(exc.value)


def test_request_url_contains_urlencoded_query(monkeypatch):
    called_urls = []

    def mock_urlopen(url, timeout=10):
        called_urls.append(url)
        return FakeResponse(b'{"status": "success", "data": {"resultType": "vector", "result": []}}')

    monkeypatch.setattr(urllib.request, "urlopen", mock_urlopen)
    source = PrometheusMetricsSource("http://prometheus:9090")
    promql = 'sum(rate(http_requests_total{app="test-service",code=~"5.."}[1m]))'
    source.query(promql)
    assert len(called_urls) == 1
    assert "http://prometheus:9090/api/v1/query?" in called_urls[0]

    parsed = urllib.parse.urlsplit(called_urls[0])
    params = urllib.parse.parse_qs(parsed.query)
    assert params.get("query") == [promql]


@pytest.mark.parametrize(
    "failure_fn",
    [
        lambda url, timeout: (_ for _ in ()).throw(
            HTTPError("http://user:secret123@prom.internal:9090", 500, "Internal Error", {}, None)
        ),
        lambda url, timeout: (_ for _ in ()).throw(
            URLError("Connection error to http://user:secret123@prom.internal:9090")
        ),
        lambda url, timeout: (_ for _ in ()).throw(TimeoutError()),
        lambda url, timeout: FakeResponse(b"<html>Error at http://user:secret123@prom.internal:9090</html>"),
        lambda url, timeout: FakeResponse(
            b'{"status": "error", "error": "failed on http://user:secret123@prom.internal:9090"}'
        ),
        lambda url, timeout: FakeResponse(
            b'{"status": "success", "data": {"resultType": "vector", "result": [{"value": [1, "1"]}, {"value": [1, "2"]}]}}'
        ),
    ],
)
def test_base_url_with_credentials_never_leaks(monkeypatch, failure_fn):
    monkeypatch.setattr(urllib.request, "urlopen", failure_fn)
    source = PrometheusMetricsSource("http://user:secret123@prom.internal:9090")
    with pytest.raises(MetricsUnavailable) as exc:
        source.query("up")
    message = str(exc.value)
    assert "secret123" not in message
    assert "user:secret123@" not in message


def test_build_metrics_source(monkeypatch):
    # Empty environment variable returns UnconfiguredMetricsSource
    monkeypatch.setenv("NETCI_PROMETHEUS_URL", "")
    source = build_metrics_source()
    assert isinstance(source, UnconfiguredMetricsSource)
    assert source.name == "none"
    with pytest.raises(MetricsUnavailable) as exc:
        source.query("up")
    assert "no metrics source is configured (NETCI_PROMETHEUS_URL is empty)" in str(exc.value)

    # Whitespace-only environment variable also returns UnconfiguredMetricsSource
    monkeypatch.setenv("NETCI_PROMETHEUS_URL", "   ")
    source = build_metrics_source()
    assert isinstance(source, UnconfiguredMetricsSource)

    # Bad scheme raises ValueError naming NETCI_PROMETHEUS_URL
    monkeypatch.setenv("NETCI_PROMETHEUS_URL", "ftp://prometheus:9090")
    with pytest.raises(ValueError) as exc:
        build_metrics_source()
    assert "NETCI_PROMETHEUS_URL" in str(exc.value)

    monkeypatch.setenv("NETCI_PROMETHEUS_URL", "localhost:9090")
    with pytest.raises(ValueError) as exc:
        build_metrics_source()
    assert "NETCI_PROMETHEUS_URL" in str(exc.value)

    # Valid http / https URL configured
    monkeypatch.setenv("NETCI_PROMETHEUS_URL", "https://prometheus.internal:9090/")
    monkeypatch.setenv("NETCI_PROMETHEUS_TIMEOUT", "15")
    configured_source = build_metrics_source()
    assert isinstance(configured_source, PrometheusMetricsSource)
    assert configured_source.name == "prometheus"
    assert configured_source.base_url == "https://prometheus.internal:9090"
    assert configured_source.timeout_seconds == 15.0
