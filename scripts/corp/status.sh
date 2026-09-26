#!/usr/bin/env bash
# What the company-shaped lab is running. Read-only.
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CONTEXT=kind-netci-corp
K=(kubectl --context "${CONTEXT}")
probe() { printf '  %-10s %s  %s\n' "$1" "$(curl -s -o /dev/null --max-time 5 -w '%{http_code}' "$2")" "$2"; }

echo "== disk (the object store turns its volumes read-only when the disk fills: backups then fail)"
used=$(df --output=pcent / | tail -1 | tr -dc 0-9)
printf '  / %s%% used, %s free%s\n' "${used}" "$(df -h --output=avail / | tail -1 | tr -d ' ')" \
  "$( (( used >= 90 )) && echo '  WARNING: free space before the next backup (docker builder prune)')"
echo "== services (HTTP status)"
probe GitLab http://172.17.0.1:8929/users/sign_in
probe Harbor http://172.17.0.1:8930/api/v2.0/ping
probe S3 http://172.17.0.1:8333/          # 403 is healthy: unsigned request refused
echo "== containers"
docker ps --format '  {{.Names}}\t{{.Status}}' | grep -E 'netci-corp-(s3|gitlab)|harbor-core|registry' || true
echo "== nodes"
"${K[@]}" get nodes -L netci.io/pool,netci.io/jenkins-controller
echo "== jenkins"
"${K[@]}" -n jenkins get pods -o wide
echo "== velero backups (newest first)"
"${ROOT}/.netci-gate/corp/bin/velero" --kubecontext "${CONTEXT}" backup get 2>/dev/null | head -6 || echo "  velero CLI not installed"
