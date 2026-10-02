#!/usr/bin/env bash
# Prometheus and Grafana on the lab (kube-prometheus-stack, lean), to check netCI's monitoring
# against the real thing: the PodMonitors select the pods, the rules load and evaluate, the
# dashboard's queries return data, and an alert fires when its failure happens. Also Loki: each
# cell's log shipper (netci-cell logShipping) sends its build logs there, so a build's log can be
# read while its cell is taken over.
#
#   lab/monitoring.sh install      then: MONITORING=on NETCI_IMAGE=... lab/helm-install.sh
#   lab/monitoring.sh uninstall
#
# Prometheus is on NodePort 30909, Grafana on 30300 (user admin; the password is generated into
# .netci-gate/lab/monitoring/ and never printed). Only what netCI's checks need: no node-exporter,
# kube-state-metrics, Alertmanager or the stack's default rules, so it fits beside the cells.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STATE="${ROOT}/.netci-gate/lab"
M="${STATE}/monitoring"
export KUBECONFIG="${STATE}/kubeconfig"
CHART_VERSION=87.10.1 LOKI_CHART_VERSION=7.3.0

case "${1:-}" in
  install)
    mkdir -m 700 -p "${M}"
    ( umask 077; [[ -s "${M}/grafana-password" ]] || openssl rand -hex 16 | tr -d '\n' > "${M}/grafana-password" )
    kubectl create namespace monitoring --dry-run=client -o yaml | kubectl apply -f - >/dev/null
    printf 'admin' > "${M}/grafana-user"
    kubectl -n monitoring create secret generic grafana-admin --from-file=admin-user="${M}/grafana-user" \
      --from-file=admin-password="${M}/grafana-password" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
    cat > "${M}/values.yaml" <<'VALUES'
defaultRules: {create: false}
alertmanager: {enabled: false}
nodeExporter: {enabled: false}
kubeStateMetrics: {enabled: false}
kubelet: {enabled: false}
kubeApiServer: {enabled: false}
kubeControllerManager: {enabled: false}
kubeScheduler: {enabled: false}
kubeProxy: {enabled: false}
kubeEtcd: {enabled: false}
coreDns: {enabled: false}
prometheusOperator:
  resources: {requests: {cpu: 50m, memory: 64Mi}, limits: {memory: 256Mi}}
prometheus:
  service: {type: NodePort, nodePort: 30909}
  prometheusSpec:
    # Every PodMonitor and PrometheusRule in the cluster, whatever its labels: netCI's charts
    # set none by default.
    podMonitorSelectorNilUsesHelmValues: false
    serviceMonitorSelectorNilUsesHelmValues: false
    ruleSelectorNilUsesHelmValues: false
    retention: 2d
    resources: {requests: {cpu: 100m, memory: 256Mi}, limits: {memory: 768Mi}}
grafana:
  admin: {existingSecret: grafana-admin, userKey: admin-user, passwordKey: admin-password}
  service: {type: NodePort, nodePort: 30300}
  resources: {requests: {cpu: 50m, memory: 128Mi}, limits: {memory: 384Mi}}
  sidecar:
    dashboards: {enabled: true, label: grafana_dashboard, labelValue: "1", searchNamespace: ALL}
  additionalDataSources:
    - {name: Loki, type: loki, uid: loki, access: proxy, url: "http://loki.monitoring.svc.cluster.local:3100"}
VALUES
    cat > "${M}/loki.yaml" <<'VALUES'
deploymentMode: SingleBinary
loki:
  auth_enabled: false
  commonConfig: {replication_factor: 1}
  storage: {type: filesystem}
  schemaConfig:
    configs:
      - {from: "2026-01-01", store: tsdb, object_store: filesystem, schema: v13, index: {prefix: index_, period: 24h}}
  # The shippers send each line's build number as structured metadata. A shipper's push is one
  # Fluent Bit chunk, up to ~2 MB, which Loki receives as up to ~5.3 MB: over Loki's default gRPC
  # limit of 4 MB, a backlog (a first install, or one after Loki was down) was refused with 500
  # and retried forever. 16 MB, and a burst to match.
  server: {grpc_server_max_recv_msg_size: 16777216, grpc_server_max_send_msg_size: 16777216}
  limits_config: {allow_structured_metadata: true, retention_period: 168h, ingestion_rate_mb: 8, ingestion_burst_size_mb: 16}
  compactor: {retention_enabled: true, delete_request_store: filesystem}
singleBinary:
  replicas: 1
  persistence: {enabled: true, size: 5Gi, storageClass: local-path}
  resources: {requests: {cpu: 50m, memory: 128Mi}, limits: {memory: 512Mi}}
backend: {replicas: 0}
read: {replicas: 0}
write: {replicas: 0}
gateway: {enabled: false}
chunksCache: {enabled: false}
resultsCache: {enabled: false}
lokiCanary: {enabled: false}
test: {enabled: false}
minio: {enabled: false}
VALUES
    helm upgrade --install loki grafana/loki --version "${LOKI_CHART_VERSION}" -n monitoring -f "${M}/loki.yaml" \
      --wait --timeout 15m
    helm upgrade --install monitoring prometheus-community/kube-prometheus-stack --version "${CHART_VERSION}" \
      -n monitoring -f "${M}/values.yaml" --wait --timeout 15m
    kubectl -n monitoring get pods ;;
  uninstall)
    helm uninstall loki -n monitoring || true
    helm uninstall monitoring -n monitoring || true
    kubectl get crd -o name | grep monitoring.coreos.com | xargs -r kubectl delete >/dev/null ;;
  *) sed -n '2,12p' "$0" >&2; exit 2 ;;
esac
