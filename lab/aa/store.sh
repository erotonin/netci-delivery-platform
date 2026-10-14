#!/usr/bin/env bash
# Active-active: netci-homesync's store moved onto an HA PostgreSQL (ADR-080).
#
#   HOMESYNC=<image@digest> lab/aa/store.sh install   operator, the cluster, the data, homesync repointed
#   lab/aa/store.sh update                             operator and cluster settings again (no data copy)
#   lab/aa/store.sh status                             instances, roles, synchronous standbys
#
# install is meant to run once: it refuses to copy data into a store that already has tables.
# The old store (netci-pg, database netci_homesync) is left as it was, so going back is pointing
# the Secret homesync-db back at it.
set -euo pipefail
cd "$(dirname "$0")/../.."
export KUBECONFIG="${KUBECONFIG:-.netci-gate/lab/kubeconfig}"
NS=aa-p2
STATE=.netci-gate/aa
CNPG_VERSION=1.30.1
# The release manifest, by checksum: it is cluster-scoped (CRDs, webhooks, RBAC), so what is
# applied must be what was read.
CNPG_SHA256=37237f145d8138256ea25ae830f87759255665ff08f8d552fdd8224a5ec032fb
OPERATOR="ghcr.io/cloudnative-pg/cloudnative-pg:${CNPG_VERSION}@sha256:923c267ec29636db3bee20f993d0ec4973fa22998e1adad37da79e4d32b5bc07"

operator() {
  local f="${STATE}/cnpg-${CNPG_VERSION}.yaml"
  [[ -f "$f" ]] || curl -sfL -o "$f" "https://raw.githubusercontent.com/cloudnative-pg/cloudnative-pg/release-${CNPG_VERSION%.*}/releases/cnpg-${CNPG_VERSION}.yaml"
  echo "${CNPG_SHA256}  ${f}" | sha256sum -c --quiet
  # --force-conflicts: the patch below owns replicas, tolerations and affinity after a first run.
  sed "s#ghcr.io/cloudnative-pg/cloudnative-pg:${CNPG_VERSION}#${OPERATOR}#g" "$f" | kubectl apply --server-side --force-conflicts -f - >/dev/null
  # Failover is the operator's decision: one operator pod on the machine that died with the
  # primary would wait ~5 minutes to be evicted before anyone promotes a standby. Two, on two
  # machines, with leader election (already on in the release). Each talks to its own machine's
  # API server (as the instances do, store.yaml): through the Service, a pooled connection to a
  # dead machine's API server delayed the operator's view by tens of seconds.
  kubectl -n cnpg-system patch deployment cnpg-controller-manager --type strategic -p '
spec:
  replicas: 2
  template:
    spec:
      containers:
        - name: manager
          env:
            - {name: KUBERNETES_SERVICE_HOST, valueFrom: {fieldRef: {fieldPath: status.hostIP}}}
            - {name: KUBERNETES_SERVICE_PORT, value: "6443"}
      affinity:
        podAntiAffinity:
          requiredDuringSchedulingIgnoredDuringExecution:
            - labelSelector: {matchLabels: {app.kubernetes.io/name: cloudnative-pg}}
              topologyKey: kubernetes.io/hostname
      tolerations:
        - {key: node.kubernetes.io/not-ready, operator: Exists, effect: NoExecute, tolerationSeconds: 10}
        - {key: node.kubernetes.io/unreachable, operator: Exists, effect: NoExecute, tolerationSeconds: 10}' >/dev/null
  kubectl -n cnpg-system rollout status deployment/cnpg-controller-manager --timeout=300s
}

psql_old() { kubectl -n netci-system exec -i netci-pg-0 -- sh -c 'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d netci_homesync -At'; }
primary() { kubectl -n "$NS" get cluster store -o jsonpath='{.status.currentPrimary}'; }
psql_new() { kubectl -n "$NS" exec -i "$(primary)" -c postgres -- psql -v ON_ERROR_STOP=1 -U postgres -d netci_homesync -At; }
counts='select (select count(*) from home_items) || '"' '"' || (select count(*) from home_changes) || '"' '"' || (select count(*) from home_build_numbers);'

install() {
  : "${HOMESYNC:?HOMESYNC=<netci image by digest> (3e9469e or later: it notices a dead primary)}"
  [[ "$HOMESYNC" == *@sha256:* ]] || { echo "HOMESYNC must be by digest" >&2; exit 1; }
  operator
  kubectl apply -f lab/aa/store.yaml >/dev/null
  kubectl -n "$NS" wait cluster/store --for=condition=Ready --timeout=900s
  if [[ "$(echo "select count(*) from pg_tables where schemaname = 'public';" | psql_new)" != 0 ]]; then
    echo "the new store already has tables: not copying over them" >&2; exit 1
  fi
  # Nothing may change while it is copied: a change after the dump would be lost.
  kubectl -n "$NS" scale deployment/homesync --replicas=0 >/dev/null
  kubectl -n "$NS" wait pod -l app=homesync --for=delete --timeout=120s
  local before after
  before=$(echo "$counts" | psql_old)
  # Owned by homesync, as the store's own migrations would have made them.
  { echo "SET ROLE homesync;"
    kubectl -n netci-system exec netci-pg-0 -- sh -c 'pg_dump -U "$POSTGRES_USER" --no-owner --no-privileges netci_homesync'
  } | psql_new >/dev/null
  after=$(echo "$counts" | psql_new)
  echo "items changes numbers: old ${before}, new ${after}"
  [[ "$before" == "$after" ]] || { echo "copy differs; homesync left stopped on the old store" >&2; exit 1; }
  # The app Secret's uri names the read-write Service; target_session_attrs keeps a connection
  # from settling on an instance that is (still, or again) a standby.
  kubectl -n "$NS" patch deployment homesync --type strategic -p "
spec:
  template:
    spec:
      containers:
        - name: homesync
          image: ${HOMESYNC}
          env:
            - {name: DATABASE_URL, valueFrom: {secretKeyRef: {name: store-app, key: uri}}}
            - {name: PGTARGETSESSIONATTRS, value: read-write}
            - {name: PGCONNECT_TIMEOUT, value: \"5\"}" >/dev/null
  kubectl -n "$NS" scale deployment/homesync --replicas=2 >/dev/null
  kubectl -n "$NS" rollout status deployment/homesync --timeout=300s
}

status() {
  kubectl -n "$NS" get pods -l cnpg.io/cluster=store -o custom-columns=POD:.metadata.name,ROLE:.metadata.labels.cnpg\\.io/instanceRole,NODE:.spec.nodeName,READY:.status.containerStatuses[0].ready
  echo "select application_name, state, sync_state from pg_stat_replication order by 1;" | psql_new
}

update() {
  operator
  kubectl apply -f lab/aa/store.yaml >/dev/null
  # The instances restart one at a time, the primary last by a switchover.
  sleep 10
  kubectl -n "$NS" wait cluster/store --for=condition=Ready --timeout=900s
}

case "${1:-}" in
  install) install ;;
  update) update ;;
  status) status ;;
  *) sed -n '2,9p' "$0"; exit 2 ;;
esac
