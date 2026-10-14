#!/usr/bin/env bash
# Active-active, the lab run by the production chart (deploy/helm/netci-jenkins-ha) with
# ci/lab-values.yaml, instead of the phase manifests applied by hand.
#
#   lab/aa/chart.sh adopt      hand the lab's existing objects to the Helm release (once)
#   lab/aa/chart.sh upgrade    helm upgrade --install, waiting for nothing (the probe watches)
#   lab/aa/chart.sh diff       what an upgrade would change, against the live objects
#
# adopt labels and annotates each object the chart renders as the release's, which is what Helm
# asks of an object it did not create; nothing else about them changes.
set -euo pipefail
cd "$(dirname "$0")/../.."
export KUBECONFIG="${KUBECONFIG:-.netci-gate/lab/kubeconfig}"
NS=aa-p2
RELEASE=jenkins-ha
CHART=deploy/helm/netci-jenkins-ha
VALUES=(-f "$CHART/ci/lab-values.yaml" ${EXTRA_VALUES:+-f "$EXTRA_VALUES"})

render() { helm template "$RELEASE" "$CHART" -n "$NS" "${VALUES[@]}"; }

adopt() {
  render | python3 -c '
import sys, yaml
for d in yaml.safe_load_all(sys.stdin):
    if d:
        print(d["kind"], d["metadata"].get("namespace", "-"), d["metadata"]["name"])' |
  while read -r kind ns name; do
    local where=(); [[ "$ns" != - ]] && where=(-n "$ns")   # "-": cluster-scoped
    kubectl "${where[@]}" get "$kind" "$name" >/dev/null 2>&1 || continue   # created by the upgrade
    kubectl "${where[@]}" label "$kind" "$name" app.kubernetes.io/managed-by=Helm --overwrite >/dev/null
    kubectl "${where[@]}" annotate "$kind" "$name" meta.helm.sh/release-name="$RELEASE" \
      meta.helm.sh/release-namespace="$NS" --overwrite >/dev/null
    echo "adopted $kind $ns/$name"
  done
}

case "${1:-}" in
  adopt) adopt ;;
  upgrade) helm upgrade --install "$RELEASE" "$CHART" -n "$NS" "${VALUES[@]}" ;;
  diff) render | kubectl diff -f - || true ;;
  *) sed -n '2,10p' "$0"; exit 2 ;;
esac
