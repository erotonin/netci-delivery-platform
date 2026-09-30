#!/usr/bin/env bash
# Deploy the spike cell onto the lab cluster. Secrets are generated into .netci-gate/lab and
# reach the cluster through files, never through a command line.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
STATE="${ROOT}/.netci-gate/lab"
CORP="${ROOT}/.netci-gate/corp"
export KUBECONFIG="${STATE}/kubeconfig"
# One manifest, several cells: CELL names the namespace, STORAGE_CLASS the JENKINS_HOME volume
# (longhorn-sync or longhorn, to measure what synchronous writes cost), NODE_PORT its address.
CELL="${CELL:-cell-a}"
STORAGE_CLASS="${STORAGE_CLASS:-longhorn-commit1}"
NODE_PORT="${NODE_PORT:-30080}"
render() { sed -e "s/cell-a/${CELL}/g" -e "s/storageClassName: longhorn-commit1/storageClassName: ${STORAGE_CLASS}/" \
  -e "s/nodePort: 30080/nodePort: ${NODE_PORT}/" "${ROOT}/lab/spike/cell.yaml"; }
( umask 077
  [[ -s "${STATE}/cell-admin-password" ]] || openssl rand -base64 24 | tr -d '\n' > "${STATE}/cell-admin-password"
  python3 - "${CORP}" "${STATE}/harbor-pull.json" <<'EOF'
import base64, json, sys
corp, out = sys.argv[1:]
user = open(f"{corp}/harbor_ops_robot_name").read().strip()
secret = open(f"{corp}/harbor_ops_robot_secret").read().strip()
auth = base64.b64encode(f"{user}:{secret}".encode()).decode()
json.dump({"auths": {"172.17.0.1:8930": {"auth": auth}}}, open(out, "w"))
EOF
)
kubectl apply -f "${ROOT}/lab/spike/storageclass-sync.yaml" -f "${ROOT}/lab/spike/storageclass-commit1.yaml" >/dev/null
render | kubectl apply -f - >/dev/null
kubectl -n "${CELL}" create secret generic harbor-pull --type kubernetes.io/dockerconfigjson \
  --from-file=.dockerconfigjson="${STATE}/harbor-pull.json" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
kubectl -n "${CELL}" create secret generic jenkins-cell \
  --from-file=admin-password="${STATE}/cell-admin-password" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
kubectl -n "${CELL}" create configmap agent-supervisor --from-file="${ROOT}/lab/spike/agent-supervisor.sh" \
  --dry-run=client -o yaml | kubectl apply -f - >/dev/null
kubectl -n "${CELL}" rollout status statefulset/jenkins --timeout=600s
