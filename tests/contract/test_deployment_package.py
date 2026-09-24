"""The Kubernetes package describes an installation the code can actually run.

Chart 1.0.0 could not start netCI and nothing noticed: it set TEMPORAL_HOST where the code
reads TEMPORAL_ADDRESS, REGISTRY_PUSH_HOST where it reads NETCI_REGISTRY_PUSH_HOST, and
omitted NETCI_AUTH_MODE and NETCI_CD_MODE, both of which production refuses to start
without. `helm lint` passed throughout. These render the chart and hold it against the code.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CHART = ROOT / "deploy" / "helm" / "netci-platform"
EXAMPLE = CHART / "examples" / "values-lab-kind.yaml"
LIBRARY_VARS = ROOT / "jenkins" / "shared-library" / "vars"

needs_helm = pytest.mark.skipif(shutil.which("helm") is None, reason="helm is not installed")


def _render(*extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["helm", "template", "netci", str(CHART), "-n", "netci-system", *extra],
        capture_output=True, text=True, timeout=60,
    )


def _code_reads() -> tuple[set[str], set[str]]:
    """Names backend/app reads literally, and the suffixes it reads after a dynamic prefix."""

    source = "\n".join(p.read_text(encoding="utf-8") for p in (ROOT / "backend" / "app").rglob("*.py"))
    q = r"[\"']"  # both quote styles: worker.py reads with single quotes
    literal = set(re.findall(rf"(?:getenv|environ\.get)\(\s*{q}([A-Z0-9_]+){q}", source))
    literal |= set(re.findall(rf"_read_secret\(\s*{q}([A-Z0-9_]+){q}", source))
    literal |= set(re.findall(rf"environ\[\s*{q}([A-Z0-9_]+){q}\s*\]", source))
    dynamic = set(re.findall(r'f"\{prefix\}_([A-Z0-9_]+)"', source))
    return literal, dynamic


@needs_helm
def test_every_setting_the_chart_renders_is_one_the_code_reads():
    import yaml

    rendered = _render("-f", str(EXAMPLE))
    assert rendered.returncode == 0, rendered.stderr
    config = next(
        doc for doc in yaml.safe_load_all(rendered.stdout)
        if doc and doc.get("kind") == "ConfigMap" and doc["metadata"]["name"].endswith("-config")
    )
    literal, dynamic = _code_reads()
    unread = []
    for key in config["data"]:
        if key in literal:
            continue
        match = re.fullmatch(r"JENKINS_[A-Z0-9_]+?_(URL|USERNAME|API_TOKEN_FILE|EXECUTORS|ID|TIMEOUT_SECONDS)", key)
        if match and (match.group(1) in dynamic or match.group(1).removesuffix("_FILE") in dynamic):
            continue
        if key.endswith("_FILE") and key.removesuffix("_FILE") in literal:
            continue  # read through _read_secret / database_url's *_FILE convention
        unread.append(key)
    assert not unread, f"the chart sets settings no code reads: {unread}"


@needs_helm
def test_the_settings_production_refuses_to_start_without_are_all_rendered():
    rendered = _render("-f", str(EXAMPLE))
    assert rendered.returncode == 0, rendered.stderr
    for required in ("NETCI_AUTH_MODE", "NETCI_CD_MODE", "NETCI_CI_MODE", "NETCI_SIGNATURE_VERIFY_MODE",
                     "NETCI_BUILD_ISOLATION", "TEMPORAL_ADDRESS", "NETCI_CALLBACK_URL", "NETCI_API_URL",
                     "DATABASE_URL_FILE", "NETCI_WORKLOAD_TOKEN_KEYS_FILE"):
        assert f"{required}:" in rendered.stdout, f"{required} is not rendered"


@needs_helm
def test_the_chart_refuses_to_render_rather_than_install_something_that_cannot_work():
    assert _render().returncode != 0
    no_dcim = _render("-f", str(EXAMPLE), "--set", "dcim.requireRevalidation=true")
    assert no_dcim.returncode != 0
    assert "dcim.provider is empty" in no_dcim.stderr


@needs_helm
def test_no_credential_is_ever_an_environment_variable():
    rendered = _render("-f", str(EXAMPLE))
    for name in ("DATABASE_URL:", "API_TOKEN:", "WORKLOAD_TOKEN_KEYS:", "PIPELINE_API_KEY:"):
        assert f"  {name}" not in rendered.stdout, f"{name} rendered as a plain setting"


def test_the_library_copy_of_the_ci_tooling_matches_its_sources():
    result = subprocess.run(["python3", str(ROOT / "scripts" / "sync_shared_library.py"), "--check"],
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr


def test_no_library_step_runs_tooling_out_of_the_checked_out_repository():
    """A real application repository has no scripts/netci_callback.py of its own."""

    offenders = [
        f"{path.name}:{number}"
        for path in LIBRARY_VARS.glob("*.groovy")
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if re.search(r"python3 [\"']?scripts/netci_callback\.py", line)
    ]
    assert not offenders, offenders


def test_the_public_entry_point_does_not_proxy_metrics():
    """The Ingress sends everything to the portal, and the portal proxies /api/* to the API.

    So /api/metrics reached the API from outside: every request ran the readiness probe
    against Jenkins, Temporal and DCIM for an anonymous caller. The portal refuses it.
    """

    nginx = (ROOT / "frontend" / "nginx.conf").read_text(encoding="utf-8")
    block = re.search(r"location = /api/metrics \{(.*?)\}", nginx, re.S)
    assert block and "return 404" in block.group(1)
    assert nginx.index("location = /api/metrics") < nginx.index("location /api/ {")


# ---------------------------------------------------------------- netCI's own SLOs


def _exported_metric_names() -> set[str]:
    source = (ROOT / "backend" / "app" / "metrics.py").read_text(encoding="utf-8")
    return set(re.findall(r'"(netci_[a-z_]+)"', source))


@needs_helm
def test_the_slo_rules_are_off_by_default_and_render_when_enabled():
    assert "PrometheusRule" not in _render("-f", str(EXAMPLE)).stdout
    rendered = _render("-f", str(EXAMPLE), "--set", "monitoring.prometheusRule.enabled=true")
    assert rendered.returncode == 0, rendered.stderr
    assert "kind: PrometheusRule" in rendered.stdout


@needs_helm
def test_every_slo_expression_names_only_metrics_netci_exports_and_every_alert_says_what_to_do():
    import yaml

    rendered = _render("-f", str(EXAMPLE), "--set", "monitoring.prometheusRule.enabled=true",
                       "--show-only", "templates/prometheusrule.yaml")
    rule = yaml.safe_load(rendered.stdout)
    exported = _exported_metric_names()
    recorded = {r["record"] for g in rule["spec"]["groups"] for r in g["rules"] if "record" in r}
    for group in rule["spec"]["groups"]:
        for item in group["rules"]:
            names = set(re.findall(r"\bnetci_[a-z_]+", item["expr"]))
            # A histogram's series carry a _bucket suffix the registry name does not.
            unknown = {n for n in names if n not in exported and n.removesuffix("_bucket") not in exported}
            assert not unknown, f"{item.get('alert') or item.get('record')}: {unknown} is not exported by netCI"
            used_records = set(re.findall(r"netci:[a-z0-9_:]+", item["expr"]))
            assert used_records <= recorded
            if "alert" in item:
                assert item["labels"]["severity"] in {"page", "ticket"}
                assert item["annotations"]["description"].strip()


@needs_helm
def test_the_burn_rate_thresholds_follow_the_availability_target():
    rendered = _render("-f", str(EXAMPLE), "--set", "monitoring.prometheusRule.enabled=true",
                       "--set", "monitoring.slo.availabilityTarget=0.99",
                       "--show-only", "templates/prometheusrule.yaml").stdout
    # 14.4x and 6x the error budget of a 99 % target.
    assert "netci:api_error_ratio:rate1h > 0.144" in rendered
    assert "netci:api_error_ratio:rate6h > 0.06" in rendered
    # Health probes are not API traffic: counted, they would bury real failures.
    assert 'route!~"/livez|/healthz|/readyz|/metrics"' in rendered
