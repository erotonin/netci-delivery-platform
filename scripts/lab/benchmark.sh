#!/usr/bin/env bash
# Benchmark the three agent modes on the lab (ADR-030). Usage: benchmark.sh [runs]
set -euo pipefail
cd "$(dirname "$0")/../.."
set -a; source .netci-gate/real-local.env; set +a
export NETCI_KUBE_CONTEXT="${NETCI_KUBE_CONTEXT:-kind-netci-local}"
ns="${NETCI_BENCHMARK_NAMESPACE:-$(PYTHONPATH=backend .venv/bin/python -c 'import uuid; from app.adapters.build_isolation import build_isolation_provisioner as p; print(p().ensure(uuid.UUID("00000000-0000-0000-0000-00000000bea7"), "netci-benchmark").namespace)')}"
T=".venv/bin/python scripts/jenkins_build.py --url $JENKINS_A_URL --username $JENKINS_A_USERNAME --token $JENKINS_A_API_TOKEN --job netci-benchmark --git-url ${NETCI_GIT_URL} --branch main --stages unit-test,build"
exec .venv/bin/python scripts/benchmark.py --runs "${1:-5}" --application-id netci-benchmark --environment "${NETCI_BENCHMARK_ENVIRONMENT:-ubuntu-24.04-kind-local}" \
  --regression-threshold "${NETCI_BENCHMARK_THRESHOLD:-25}" \
  --report evidence/benchmarks/report.json --csv evidence/benchmarks/samples.csv \
  --baseline-command "$T --label netci-shared" \
  --ephemeral-command "$T --label netci-ephemeral" \
  --isolated-command "$T --label netci-ephemeral --isolation-namespace $ns --cache-claim netci-cache"
