#!/usr/bin/env bash
# Deploy the spike cell onto the lab cluster. Secrets are generated into .netci-gate/lab and
# reach the cluster through files, never through a command line.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
STATE="${ROOT}/.netci-gate/lab"
CORP="${ROOT}/.netci-gate/corp"
export KUBECONFIG="${STATE}/kubeconfig"
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
kubectl apply -f "${ROOT}/lab/spike/cell.yaml" >/dev/null
kubectl -n cell-a create secret generic harbor-pull --type kubernetes.io/dockerconfigjson \
  --from-file=.dockerconfigjson="${STATE}/harbor-pull.json" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
kubectl -n cell-a create secret generic jenkins-cell \
  --from-file=admin-password="${STATE}/cell-admin-password" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
kubectl -n cell-a create configmap agent-supervisor --from-file="${ROOT}/lab/spike/agent-supervisor.sh" \
  --dry-run=client -o yaml | kubectl apply -f - >/dev/null
kubectl -n cell-a rollout status statefulset/jenkins --timeout=600s
