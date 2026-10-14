#!/usr/bin/env bash
# Active-active: netci-homesync's store moved onto an HA PostgreSQL (ADR-080).
#
#   HOMESYNC=<image@digest> lab/aa/store.sh install   operator, the cluster, the data, homesync repointed
#   lab/aa/store.sh update                             operator and cluster settings again (no data copy)
#   lab/aa/store.sh plugin                             cert-manager and the Barman Cloud plugin (from Harbor)
#   lab/aa/store.sh backup                             backups to the lab's S3, and a base backup now
#   lab/aa/store.sh restore-test                       recover a copy from the backups and compare it
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

# The Barman Cloud plugin, which replaces the in-tree barmanObjectStore (removed in CloudNativePG
# 1.31), and cert-manager, which it needs for the TLS between the operator and itself. Both by
# checksum, as the operator is; their images are Harbor's copies (the nodes pull from Harbor), by
# digest, the plugin's sidecar included -- the plugin injects it into every instance pod.
CERT_MANAGER_VERSION=v1.21.2
CERT_MANAGER_SHA256=e03b668ec8675214af6b0a671699d088f2601fa3878e0dbe1b41d3feafd1879f
BARMAN_VERSION=v0.15.1
BARMAN_SHA256=8f377ff4a7d0d3f74fc64b0f7ac4f449fc38ca9e83a40107cca2f9315a9bd920
HARBOR=172.17.0.1:8930/netci

fetch() {  # url file sha256
  [[ -f "$2" ]] || curl -sfL -o "$2" "$1"
  echo "$3  $2" | sha256sum -c --quiet
}

# The pull secret into another namespace, through a pipe: its value never reaches a command line.
pull_secret() {
  kubectl -n "$NS" get secret harbor-pull -o json | python3 -c '
import json, sys
s = json.load(sys.stdin); m = s["metadata"]
s["metadata"] = {"name": m["name"], "namespace": sys.argv[1]}
print(json.dumps(s))' "$1" | kubectl apply -f - >/dev/null
}

# Each image of a manifest replaced by Harbor's copy, by digest (.netci-gate/aa/mirror-digests:
# "<upstream ref> <digest>" per line), and the namespace's pods given the pull secret.
from_harbor() {  # manifest
  python3 - "$1" "${STATE}/mirror-digests" "$HARBOR" <<'PY'
import base64, sys, yaml
manifest, digests, harbor = sys.argv[1:]
mirror = {}
for line in open(digests):
    ref, digest = line.split()
    mirror[ref] = f"{harbor}/{ref.rsplit('/', 1)[1].split(':')[0]}@{digest}"
docs = [d for d in yaml.safe_load_all(open(manifest)) if d]
for d in docs:
    if d["kind"] == "Deployment":
        pod = d["spec"]["template"]["spec"]
        pod["imagePullSecrets"] = [{"name": "harbor-pull"}]
        for c in pod["containers"]:
            c["image"] = mirror[c["image"]]
    if d["kind"] == "Secret" and "SIDECAR_IMAGE" in d.get("data", {}):
        ref = base64.b64decode(d["data"]["SIDECAR_IMAGE"]).decode().strip()
        d["data"]["SIDECAR_IMAGE"] = base64.b64encode(mirror[ref].encode()).decode()
print(yaml.safe_dump_all(docs))
PY
}

plugin() {
  local cm="${STATE}/cert-manager-${CERT_MANAGER_VERSION}.yaml" bc="${STATE}/plugin-barman-cloud-${BARMAN_VERSION}.yaml"
  fetch "https://github.com/cert-manager/cert-manager/releases/download/${CERT_MANAGER_VERSION}/cert-manager.yaml" "$cm" "$CERT_MANAGER_SHA256"
  fetch "https://github.com/cloudnative-pg/plugin-barman-cloud/releases/download/${BARMAN_VERSION}/manifest.yaml" "$bc" "$BARMAN_SHA256"
  kubectl create namespace cert-manager --dry-run=client -o yaml | kubectl apply -f - >/dev/null
  pull_secret cert-manager
  pull_secret cnpg-system
  from_harbor "$cm" | kubectl apply --server-side --force-conflicts -f - >/dev/null
  kubectl -n cert-manager rollout status deployment/cert-manager-webhook --timeout=300s
  from_harbor "$bc" | kubectl apply --server-side --force-conflicts -f - >/dev/null
  # The plugin is on the failover path: while the operator cannot reach it, it reconciles nothing
  # for the cluster ("cannot proceed to reconciliation due to an error while interacting with
  # plugins"). Measured: its one pod stopped and the primary deleted, no standby was promoted in
  # 189 s; started again, a primary 16 s later. So two, on two machines, as the operator is, with
  # leader election (in the release) and the same tolerations and API server choice.
  # Only the leader serves (port 9090, which the readiness probe checks): the other pod is never
  # Ready until it takes the lease. The release updates it by Recreate, which is right: a rolling
  # update would stall (the new pod cannot become Ready while the old leader holds the lease, and
  # the old leader is not removed until it does). Recreate leaves the store without failover for
  # the seconds between the old pods stopping and a new one serving.
  # The primary's machine lost with the plugin's leader on it, the operator called the dead pod
  # through the Service until the other took the lease (15 s) and passed its readiness check
  # (every 10 s in the release): edits refused 74 s against 39 s with the leader elsewhere.
  # Checked every 2 s instead.
  kubectl -n cnpg-system patch deployment barman-cloud --type strategic -p '
spec:
  replicas: 2
  template:
    spec:
      containers:
        - name: barman-cloud
          readinessProbe: {periodSeconds: 2, initialDelaySeconds: 2}
          env:
            - {name: KUBERNETES_SERVICE_HOST, valueFrom: {fieldRef: {fieldPath: status.hostIP}}}
            - {name: KUBERNETES_SERVICE_PORT, value: "6443"}
      affinity:
        podAntiAffinity:
          requiredDuringSchedulingIgnoredDuringExecution:
            - labelSelector: {matchLabels: {app: barman-cloud}}
              topologyKey: kubernetes.io/hostname
      tolerations:
        - {key: node.kubernetes.io/not-ready, operator: Exists, effect: NoExecute, tolerationSeconds: 10}
        - {key: node.kubernetes.io/unreachable, operator: Exists, effect: NoExecute, tolerationSeconds: 10}' >/dev/null
  local i
  for i in $(seq 1 100); do
    [[ "$(kubectl -n cnpg-system get deployment barman-cloud -o jsonpath='{.status.updatedReplicas} {.status.readyReplicas}')" == "2 1" ]] && break
    sleep 3
  done
  kubectl -n cnpg-system get pods -l app=barman-cloud -o custom-columns=POD:.metadata.name,NODE:.spec.nodeName,SERVING:.status.containerStatuses[0].ready
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

# Backups to the lab's S3 (SeaweedFS, scripts/corp/up.sh): the credentials as a Secret
# (from the files, never the command line), and a first base backup now.
backup() {
  local C=.netci-gate/corp S=.netci-gate/aa/s3
  # The key files end in a newline, which went into the S3 signature's header ("Invalid header
  # value"); trimmed copies, mode 600, so the values never reach a command line.
  ( umask 077; mkdir -p "$S"
    tr -d '\r\n' < "$C/s3-access-key" > "$S/access-key"
    tr -d '\r\n' < "$C/s3-secret-key" > "$S/secret-key" )
  # The lab's S3 identity may write to netci-jenkins-backups only (scripts/corp/up.sh): the store's
  # backups go under their own prefix there.
  kubectl -n "$NS" create secret generic store-s3 --from-file=ACCESS_KEY_ID="$S/access-key" \
    --from-file=ACCESS_SECRET_KEY="$S/secret-key" --from-file=ca.crt="$C/pki/ca.crt" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
  # The store's backup settings are the chart's now (lab/aa/chart.sh), not store.yaml's.
  kubectl -n "$NS" wait cluster/store --for=jsonpath='{.status.conditions[?(@.type=="ContinuousArchiving")].status}'=True --timeout=300s
  local name="store-now-$(date -u +%Y%m%d%H%M%S)"
  kubectl -n "$NS" apply -f - >/dev/null <<YAML
apiVersion: postgresql.cnpg.io/v1
kind: Backup
metadata: {name: ${name}, namespace: ${NS}}
spec: {cluster: {name: store}$(plugin_mode && echo ', method: plugin, pluginConfiguration: {name: barman-cloud.cloudnative-pg.io}')}
YAML
  kubectl -n "$NS" wait "backups.postgresql.cnpg.io/${name}" --for=jsonpath='{.status.phase}'=completed --timeout=600s
  echo "base backup ${name} completed"
}

# Through the Barman Cloud plugin (the chart's backup.mode plugin) or the in-tree barmanObjectStore.
plugin_mode() { [[ "$(kubectl -n "$NS" get cluster store -o jsonpath='{.spec.plugins[*].name}')" == *barman-cloud* ]]; }

# A restore that is not tried is not a backup: a new one-instance cluster recovered from the object
# store, its rows compared with the store's, then removed. Exit 1 when they differ.
restore_test() {
  kubectl -n "$NS" delete cluster store-restore --ignore-not-found --wait=true >/dev/null
  kubectl -n "$NS" apply -f - >/dev/null <<YAML
apiVersion: postgresql.cnpg.io/v1
kind: Cluster
metadata: {name: store-restore, namespace: ${NS}}
spec:
  instances: 1
  imageName: $(kubectl -n "$NS" get cluster store -o jsonpath='{.spec.imageName}')
  imagePullSecrets: [{name: harbor-pull}]
  storage: {storageClass: local-path, size: 2Gi}
  resources: {requests: {cpu: 50m, memory: 128Mi}, limits: {memory: 384Mi}}
  env:
    - {name: KUBERNETES_SERVICE_HOST, valueFrom: {fieldRef: {fieldPath: status.hostIP}}}
    - {name: KUBERNETES_SERVICE_PORT, value: "6443"}
  bootstrap:
    recovery: {source: store}
  externalClusters:
    - name: store
$(if plugin_mode; then echo '      plugin:
        name: barman-cloud.cloudnative-pg.io
        parameters: {barmanObjectName: store-backups, serverName: store}'; else echo '      barmanObjectStore:
        serverName: store
        destinationPath: s3://netci-jenkins-backups/netci-store/aa-p2
        endpointURL: https://172.17.0.1:8333
        endpointCA: {name: store-s3, key: ca.crt}
        s3Credentials:
          accessKeyId: {name: store-s3, key: ACCESS_KEY_ID}
          secretAccessKey: {name: store-s3, key: ACCESS_SECRET_KEY}
        wal: {maxParallel: 2}'; fi)
YAML
  local t0=$SECONDS
  kubectl -n "$NS" wait cluster/store-restore --for=condition=Ready --timeout=900s >/dev/null
  local q="select (select count(*) from home_items) || ' ' || (select count(*) from home_build_numbers);"
  local live restored
  live=$(echo "$q" | kubectl -n "$NS" exec -i "$(primary)" -c postgres -- psql -U postgres -d netci_homesync -At)
  restored=$(echo "$q" | kubectl -n "$NS" exec -i store-restore-1 -c postgres -- psql -U postgres -d netci_homesync -At)
  echo "restored in $((SECONDS - t0)) s; items and numbers: store ${live}, restored ${restored}"
  kubectl -n "$NS" delete cluster store-restore --wait=false >/dev/null
  # The restore is to the end of the archived WAL: rows written since may be missing, never extra.
  [[ "${restored%% *}" -le "${live%% *}" && "${restored%% *}" -gt 0 ]]
}

status() {
  kubectl -n "$NS" get pods -l cnpg.io/cluster=store -o custom-columns=POD:.metadata.name,ROLE:.metadata.labels.cnpg\\.io/instanceRole,NODE:.spec.nodeName,READY:.status.containerStatuses[0].ready
  echo "select application_name, state, sync_state from pg_stat_replication order by 1;" | psql_new
}

update() {
  operator
  lab/aa/chart.sh upgrade >/dev/null
  # The instances restart one at a time, the primary last by a switchover.
  sleep 10
  kubectl -n "$NS" wait cluster/store --for=condition=Ready --timeout=900s
}

case "${1:-}" in
  install) install ;;
  plugin) plugin ;;
  update) update ;;
  backup) backup ;;
  restore-test) restore_test ;;
  status) status ;;
  *) sed -n '2,10p' "$0"; exit 2 ;;
esac
