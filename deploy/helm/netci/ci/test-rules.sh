#!/usr/bin/env bash
# Render the chart's PrometheusRule and run promtool's checks and unit tests on it
# (ci/rules-test.yaml). Needs Docker for prom/prometheus, which carries promtool.
set -euo pipefail
CI="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
W="$(mktemp -d)"; trap 'rm -rf "${W}"' EXIT
chmod 755 "${W}"  # promtool runs as nobody in its image
helm template n "${CI}/.." -n netci-system -f "${CI}/lab-values.yaml" --set monitoring.enabled=true \
  --set image.digest="sha256:$(printf '0%.0s' $(seq 64))" |
  python3 -c 'import sys, yaml
for d in yaml.safe_load_all(sys.stdin):
    if d and d["kind"] == "PrometheusRule":
        open(sys.argv[1] + "/full.yaml", "w").write(yaml.safe_dump(d["spec"]))
        # The tests check when an alert fires and with which labels; promtool would also
        # compare every annotation, which would only restate the summaries.
        for g in d["spec"]["groups"]:
            for r in g["rules"]:
                r.pop("annotations", None)
        open(sys.argv[1] + "/rules.yaml", "w").write(yaml.safe_dump(d["spec"]))' "${W}"
cp "${CI}/rules-test.yaml" "${W}/" && chmod 644 "${W}"/*
promtool() { docker run --rm --network none -v "${W}:/w:ro" -w /w --entrypoint promtool "${PROMETHEUS_IMAGE:-prom/prometheus:v3.5.0}" "$@"; }
promtool check rules full.yaml
promtool test rules rules-test.yaml
