#!/usr/bin/env bash
# HAProxy in front of the two API replicas: :8000 -> :8100/:8101, stats on :8404.
#   lb.sh up | down | status
set -euo pipefail
cd "$(dirname "$0")/../.."
case "${1:-up}" in
  up)
    docker rm -f netci-lb >/dev/null 2>&1 || true
    docker run -d --name netci-lb --network host --restart unless-stopped \
      -v "$PWD/infra/lb/haproxy.cfg:/usr/local/etc/haproxy/haproxy.cfg:ro" haproxy:3.0-alpine >/dev/null
    for _ in $(seq 1 20); do curl -sf http://127.0.0.1:8000/livez >/dev/null && break; sleep 1; done
    "$0" status ;;
  down) docker rm -f netci-lb >/dev/null 2>&1 || true ;;
  status)
    curl -s 'http://127.0.0.1:8404/;csv' | awk -F, 'NR>1 && $1=="netci_api_replicas" && $2!="BACKEND" {printf "%-8s %s\n", $2, $18}' ;;
  *) echo "usage: $0 up|down|status" >&2; exit 2 ;;
esac
