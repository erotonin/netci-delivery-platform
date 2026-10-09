#!/usr/bin/env bash
# Lint and render both Helm charts, and check that they refuse what they must (CI, and by hand).
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
D="sha256:$(printf '0%.0s' $(seq 64))"
P=(-f "${ROOT}/deploy/helm/netci/ci/lab-values.yaml" --set image.digest="${D}")
C=(-f "${ROOT}/deploy/helm/netci-cell/ci/lab-values.yaml" --set netci.digest="${D}")
helm lint "${ROOT}/deploy/helm/netci" "${P[@]}"
helm lint "${ROOT}/deploy/helm/netci-cell" "${C[@]}"
helm template n "${ROOT}/deploy/helm/netci" -n netci-system "${P[@]}" --set monitoring.enabled=true >/dev/null
helm template c "${ROOT}/deploy/helm/netci-cell" -n cell-a "${C[@]}" --set kubernetesPlugin.enabled=true \
  --set logShipping.enabled=true --set logShipping.lokiUrl=http://loki:3100 \
  --set logShipping.image=cr.fluentbit.io/fluent/fluent-bit@sha256:c5542543523c9678398dd78d927c05e8425ec15b038f226b67b3b019b1a70845 >/dev/null
refuses() {  # refuses <what> <expected message> helm args...
  local what=$1 want=$2; shift 2
  if out=$(helm template x "$@" 2>&1); then echo "FAIL: rendered $what" >&2; exit 1; fi
  grep -q -- "${want}" <<<"${out}" || { echo "FAIL: $what refused for another reason: ${out}" >&2; exit 1; }
  echo "ok: refuses $what"
}
refuses "the platform without a digest" "digest" "${ROOT}/deploy/helm/netci" -f "${ROOT}/deploy/helm/netci/ci/lab-values.yaml" --set image.digest=
refuses "a cell without a digest" "netci.digest" "${ROOT}/deploy/helm/netci-cell" -f "${ROOT}/deploy/helm/netci-cell/ci/lab-values.yaml" --set netci.digest=
refuses "a toleration that is not a number" "outOfServiceTolerationSeconds" "${ROOT}/deploy/helm/netci-cell" "${C[@]}" --set outOfServiceTolerationSeconds=soon
refuses "log shipping without a pinned image" "pinned by digest" "${ROOT}/deploy/helm/netci-cell" "${C[@]}" --set logShipping.enabled=true --set logShipping.lokiUrl=http://loki:3100 --set logShipping.image=fluent-bit:latest
echo "charts: ok"
