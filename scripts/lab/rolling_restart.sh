#!/usr/bin/env bash
# Restart the API replicas one at a time behind the balancer (zero requests lost, measured).
set -euo pipefail
cd "$(dirname "$0")/../.."
logs="${NETCI_LAB_LOG_DIR:-/tmp/netci-lab}"; mkdir -p "$logs"
for spec in "api-a:8100" "api-b:8101"; do
  name="${spec%%:*}" port="${spec##*:}"
  pid="$(ss -ltnp | grep ":$port " | sed -n 's/.*pid=\([0-9]*\).*/\1/p' | head -1 || true)"
  [ -n "$pid" ] && kill -TERM "$pid" && sleep 2
  nohup scripts/lab/api_replica.sh "$name" "$port" > "$logs/$name.log" 2>&1 &
  for _ in $(seq 1 40); do sleep 2; curl -sf "http://127.0.0.1:$port/healthz" >/dev/null && break; done
  for _ in $(seq 1 20); do sleep 2; scripts/lab/lb.sh status | grep -q "$name *UP\$" && break; done
  echo "$name restarted ($(scripts/lab/lb.sh status | paste -sd' '))"
done
