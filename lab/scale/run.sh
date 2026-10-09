#!/usr/bin/env bash
# One scale run on a fresh kwok cluster: NODES machines, CELLS cells, KILLS machines lost.
#
#   lab/scale/run.sh 100 300 3        (kwokctl and kwok 0.8.0 in $KWOK_DIR; evidence in lab/evidence)
#
# kwok runs kube-apiserver, etcd, kube-scheduler and kube-controller-manager v1.36.1 as local
# processes, with its own node leases off (lab/scale/fleet renews them, as kubelets would).
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
: "${KWOK_DIR:?the directory holding kwokctl and kwok}"
NODES=${1:-100} CELLS=${2:-300} KILLS=${3:-3} SETTLE=${SETTLE:-90s}
W="$(mktemp -d)"
cat > "${W}/kwok.yaml" <<'YAML'
apiVersion: config.kwok.x-k8s.io/v1alpha1
kind: KwokctlConfiguration
options:
  nodeStatusUpdateFrequencyMilliseconds: 10000
componentsPatches:
  - name: kwok-controller
    extraArgs:
      - key: node-lease-duration-seconds
        value: "0"
YAML
"${KWOK_DIR}/kwokctl" delete cluster --name netci-scale >/dev/null 2>&1 || true
"${KWOK_DIR}/kwokctl" create cluster --name netci-scale --runtime binary --kwok-controller-binary "${KWOK_DIR}/kwok" \
  --config "${W}/kwok.yaml" --wait 5m >/dev/null
"${KWOK_DIR}/kwokctl" get kubeconfig --name netci-scale > "${W}/kubeconfig"
(cd "${ROOT}" && go build -o "${W}/supervisor" ./cmd/supervisor && go build -o "${W}/fleet" ./lab/scale/fleet)
"${W}/fleet" -kubeconfig "${W}/kubeconfig" -supervisor "${W}/supervisor" -nodes "${NODES}" -cells "${CELLS}" \
  -kills "${KILLS}" -settle "${SETTLE}" -out "${ROOT}/lab/evidence"
