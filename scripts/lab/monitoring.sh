#!/usr/bin/env bash
# Prometheus + Alertmanager for the lab (host network: 9090 / 9093), plus a webhook
# receiver on 9095 that appends every alert notification to $NETCI_LAB_LOG_DIR/alerts.jsonl.
#   monitoring.sh up | down | alerts
set -euo pipefail
cd "$(dirname "$0")/../.."
root="$PWD"; logs="${NETCI_LAB_LOG_DIR:-/tmp/netci-lab}"; mkdir -p "$logs" .netci-gate/monitoring
case "${1:-up}" in
  up)
    sed "s#\${NETCI_ALERT_WEBHOOK_URL}#${NETCI_ALERT_WEBHOOK_URL:-http://127.0.0.1:9095/alerts}#" infra/monitoring/alertmanager.yml > .netci-gate/monitoring/alertmanager.yml
    docker rm -f netci-prometheus netci-alertmanager >/dev/null 2>&1 || true
    docker run -d --name netci-alertmanager --network host --restart unless-stopped \
      -v "$root/.netci-gate/monitoring/alertmanager.yml:/etc/alertmanager/alertmanager.yml:ro" \
      prom/alertmanager:v0.28.1 --config.file=/etc/alertmanager/alertmanager.yml --web.listen-address=127.0.0.1:9093 --cluster.listen-address= >/dev/null
    docker run -d --name netci-prometheus --network host --restart unless-stopped \
      -v "$root/infra/monitoring/prometheus.yml:/etc/prometheus/prometheus.yml:ro" \
      -v "$root/infra/monitoring/rules.yml:/etc/prometheus/rules.yml:ro" \
      prom/prometheus:v3.5.0 --config.file=/etc/prometheus/prometheus.yml --web.listen-address=127.0.0.1:9090 >/dev/null
    pkill -f "[a]lert_webhook_receiver.py" || true
    nohup .venv/bin/python scripts/lab/alert_webhook_receiver.py --port 9095 --out "$logs/alerts.jsonl" > "$logs/alert-receiver.log" 2>&1 &
    sleep 3
    curl -sf http://127.0.0.1:9090/-/ready && curl -sf http://127.0.0.1:9093/-/ready && echo "prometheus :9090, alertmanager :9093, receiver :9095 -> $logs/alerts.jsonl"
    ;;
  down) docker rm -f netci-prometheus netci-alertmanager >/dev/null 2>&1 || true; pkill -f "[a]lert_webhook_receiver.py" || true ;;
  alerts) curl -s http://127.0.0.1:9093/api/v2/alerts | python3 -c "import json,sys; [print(a['labels']['alertname'], a['labels'].get('replica',''), a['status']['state'], a['startsAt']) for a in json.load(sys.stdin)]" ;;
  *) echo "usage: $0 up|down|alerts" >&2; exit 2 ;;
esac
