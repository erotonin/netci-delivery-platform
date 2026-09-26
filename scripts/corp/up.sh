#!/usr/bin/env bash
# Bring up the company-shaped lab (ADR-055/056/057) from nothing, or finish a partial one.
# Every step is idempotent: re-running it skips what already exists.
#
#   scripts/corp/up.sh
#
# What it builds:
#   kind cluster netci-corp   3 control-plane + 3 workers (infra/corp/kind-corp.yaml)
#   SeaweedFS  172.17.0.1:8333  S3 for Velero (MinIO no longer publishes community images)
#   GitLab     172.17.0.1:8929  group `platform`: shared library + payments-api
#   Harbor     172.17.0.1:8930  projects netci, mirror, apps; robot account `netci`
#   Velero     Kopia file-system backup of namespace jenkins every 15 minutes
#   Jenkins    one controller (StatefulSet), JCasC, JENKINS_HOME on a local PV
#
# Secrets are generated into .netci-gate/corp (mode 700) and never printed. The cosign key
# is the live lab's (.netci-gate/jenkins/secrets/NETCI_COSIGN_PRIVATE_KEY), so signatures
# verify with the same public key.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
C="${ROOT}/.netci-gate/corp"
CONTEXT=kind-netci-corp
K=(kubectl --context "${CONTEXT}")
HARBOR_PUSH=localhost:8930           # the host pushes here; nodes pull 172.17.0.1:8930
CHART_VERSION=5.9.64
AGENT_IMAGE=jenkins/inbound-agent:3386.v353e57a_1b_ea_0-1-jdk21
CONTROLLER_IMAGE=netci/jenkins-controller:2.541.1-netci1
TOOLBOX_IMAGE=netci/ci-toolbox:0.4.0
log() { printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*"; }
secret() {  # generate once, never overwrite: services were initialised with the first value
  [[ -s "${C}/$1" ]] || ( umask 077; python3 -c 'import secrets;print(secrets.token_urlsafe(24))' > "${C}/$1" )
}

mkdir -p "${C}" && chmod 700 "${C}"
for name in gitlab_root_password harbor_admin_password harbor_db_password jenkins_admin_password jenkins_netci_sa_password; do secret "${name}"; done

# kind nodes run many inotify watchers; the kernel default (128 instances) crash-loops pods.
if (( $(sysctl -n fs.inotify.max_user_instances) < 1024 )); then
  echo "fs.inotify.max_user_instances is below 1024; see /etc/sysctl.d/99-netci-kind.conf" >&2; exit 1
fi

# SeaweedFS marks its volumes read-only on a nearly full disk and every backup then fails
# with a 500 (seen 2026-09-26 at 100%); refuse to start rather than run into it.
free_gb=$(( $(df --output=avail -k / | tail -1) / 1024 / 1024 ))
(( free_gb >= 10 )) || { echo "only ${free_gb} GB free on /: free space first (docker builder prune)" >&2; exit 1; }

log "lab CA and certificates"
"${ROOT}/scripts/corp/lab_ca.sh" ca >/dev/null
[[ -s "${C}/pki/s3.crt" ]] || "${ROOT}/scripts/corp/lab_ca.sh" issue s3 IP:172.17.0.1 DNS:netci-corp-s3 DNS:localhost IP:127.0.0.1 >/dev/null
[[ -s "${C}/pki/harbor.crt" ]] || "${ROOT}/scripts/corp/lab_ca.sh" issue harbor IP:172.17.0.1 DNS:localhost IP:127.0.0.1 >/dev/null
# Node registry configuration (containerd hosts.toml), mounted into every kind node.
certs_d="${C}/containerd-certs.d"
mkdir -p "${certs_d}/172.17.0.1:8930" "${certs_d}/172.17.0.1:55000"
cp "${C}/pki/ca.crt" "${certs_d}/172.17.0.1:8930/ca.crt"
printf 'server = "https://172.17.0.1:8930"\n\n[host."https://172.17.0.1:8930"]\n  capabilities = ["pull", "resolve"]\n  ca = "/etc/containerd/certs.d/172.17.0.1:8930/ca.crt"\n' > "${certs_d}/172.17.0.1:8930/hosts.toml"
printf 'server = "http://172.17.0.1:55000"\n\n[host."http://172.17.0.1:55000"]\n  capabilities = ["pull", "resolve"]\n' > "${certs_d}/172.17.0.1:55000/hosts.toml"
# Docker on this host pushes to Harbor and must trust the same CA.
for h in 172.17.0.1:8930 localhost:8930; do sudo -n install -D -m 644 "${C}/pki/ca.crt" "/etc/docker/certs.d/${h}/ca.crt"; done

log "kind cluster netci-corp"
if ! kind get clusters 2>/dev/null | grep -qx netci-corp; then
  sed "s#__CERTS_D__#${certs_d}#g" "${ROOT}/infra/corp/kind-corp.yaml" > "${C}/kind-corp.rendered.yaml"
  kind create cluster --config "${C}/kind-corp.rendered.yaml"
fi
# An HA kind cluster's API goes through this haproxy container, and kind creates it without a
# restart policy that survives a host reboot: after one, every kubectl call is refused and the
# workers go NotReady (seen 2026-09-26).
docker update --restart unless-stopped netci-corp-external-load-balancer >/dev/null
docker start netci-corp-external-load-balancer >/dev/null

log "SeaweedFS (S3, behind a TLS gateway)"
for f in s3-access-key s3-secret-key; do
  [[ -s "${C}/${f}" ]] || ( umask 077; python3 -c 'import secrets;print(secrets.token_hex(20))' > "${C}/${f}" )
done
if [[ ! -s "${C}/s3.json" ]]; then
  python3 - "${C}" <<'EOF'
import json, os, sys
c = sys.argv[1]
ak, sk = (open(f"{c}/{n}").read().strip() for n in ("s3-access-key", "s3-secret-key"))
# Least privilege: Velero's identity can use its one bucket and nothing else (no Admin).
bucket = "netci-jenkins-backups"
cfg = {"identities": [{"name": "velero", "credentials": [{"accessKey": ak, "secretKey": sk}],
                       "actions": [f"{a}:{bucket}" for a in ("Read", "Write", "List", "Tagging")]}]}
with open(f"{c}/s3.json", "w") as fh:
    json.dump(cfg, fh)
EOF
  # Read by the container's non-root user through a bind mount; the directory stays 700.
  chmod 644 "${C}/s3.json"
fi
docker compose -f "${ROOT}/infra/corp/seaweedfs/docker-compose.yml" up -d >/dev/null
for _ in $(seq 30); do [[ "$(curl -s -o /dev/null -m 5 -w '%{http_code}' --cacert "${C}/pki/ca.crt" https://172.17.0.1:8333/)" == 403 ]] && break; sleep 2; done
# Velero does not create its bucket. The name is the one every backup location points at.
docker exec netci-corp-s3 sh -c 'echo "s3.bucket.list" | weed shell 2>/dev/null' | grep -q "netci-jenkins-backups" \
  || docker exec netci-corp-s3 sh -c 'echo "s3.bucket.create -name netci-jenkins-backups" | weed shell >/dev/null 2>&1'

log "GitLab (first start takes several minutes)"
docker compose -f "${ROOT}/infra/corp/gitlab/docker-compose.yml" up -d >/dev/null
for _ in $(seq 120); do
  [[ "$(curl -s -o /dev/null -w '%{http_code}' http://172.17.0.1:8929/users/sign_in)" == 200 ]] && break; sleep 5
done
bash "${ROOT}/infra/corp/gitlab/bootstrap.sh"

log "Harbor"
if [[ -d "${C}/harbor-installer/harbor" ]] && ! curl -fsS -o /dev/null http://172.17.0.1:8930/api/v2.0/ping 2>/dev/null; then
  # Installed but down: after a reboot its containers start before harbor-log, whose syslog
  # they log to, exit 128 and are not retried. Starting the project again fixes that.
  ( cd "${C}/harbor-installer/harbor" && sudo -n docker compose up -d >/dev/null )
  for _ in $(seq 30); do curl -fsS -o /dev/null http://172.17.0.1:8930/api/v2.0/ping 2>/dev/null && break; sleep 3; done
fi
curl -fsS -o /dev/null http://172.17.0.1:8930/api/v2.0/ping 2>/dev/null || bash "${ROOT}/infra/corp/harbor/install.sh"
bash "${ROOT}/infra/corp/harbor/bootstrap.sh"

log "images into Harbor (nodes have no route to Docker Hub)"
# The operators' robot: the builds' robot can push only to `apps` (infra/corp/harbor/bootstrap.sh).
docker login "${HARBOR_PUSH}" -u "$(<"${C}/harbor_ops_robot_name")" --password-stdin < "${C}/harbor_ops_robot_secret" >/dev/null
mirror() {  # $1 = upstream image; pushed under mirror/<same path>
  local short="${1#docker.io/}"; short="${short#quay.io/}"
  docker pull -q "$1" >/dev/null && docker tag "$1" "${HARBOR_PUSH}/mirror/${short}" && docker push -q "${HARBOR_PUSH}/mirror/${short}" >/dev/null
}
helm repo add jenkins https://charts.jenkins.io >/dev/null 2>&1 || true
helm repo update jenkins >/dev/null
sidecar=$(helm show values jenkins/jenkins --version "${CHART_VERSION}" | python3 -c '
import sys, yaml
s = yaml.safe_load(sys.stdin)["controller"]["sidecars"]["configAutoReload"]["image"]
print("{}/{}:{}".format(s.get("registry", "docker.io"), s["repository"], s["tag"]))')
for img in velero/velero:v1.18.3 velero/velero-plugin-for-aws:v1.14.3 "${sidecar}" "${AGENT_IMAGE}"; do mirror "${img}"; done
docker build -q --network host -t "${HARBOR_PUSH}/${CONTROLLER_IMAGE}" -f "${ROOT}/jenkins/Dockerfile.controller" "${ROOT}/jenkins" >/dev/null
docker build -q --network host -t "${HARBOR_PUSH}/${TOOLBOX_IMAGE}" "${ROOT}/jenkins/agent-toolbox" >/dev/null
docker push -q "${HARBOR_PUSH}/${CONTROLLER_IMAGE}" >/dev/null
docker push -q "${HARBOR_PUSH}/${TOOLBOX_IMAGE}" >/dev/null

log "Trivy DB mirror, refreshed every 6 hours by a user timer"
"${ROOT}/scripts/corp/mirror_trivy_db.sh"
mkdir -p "${HOME}/.config/systemd/user"
cp "${ROOT}"/infra/corp/systemd/netci-trivy-db-mirror.{service,timer} "${HOME}/.config/systemd/user/"
systemctl --user daemon-reload && systemctl --user enable --now netci-trivy-db-mirror.timer >/dev/null

log "StorageClass local-backup and Velero"
"${K[@]}" apply -f "${ROOT}/infra/corp/storage-local-backup.yaml" >/dev/null
"${K[@]}" -n velero get deploy velero >/dev/null 2>&1 || bash "${ROOT}/infra/corp/velero/install.sh"

log "Jenkins"
"${K[@]}" create namespace jenkins --dry-run=client -o yaml | "${K[@]}" apply -f - >/dev/null
"${K[@]}" apply -f "${ROOT}/infra/kind/jenkins-agent-rbac.yaml" >/dev/null
# The chart's controller service account creates the build pods in netci-build.
"${K[@]}" -n netci-build create rolebinding jenkins-chart-controller --role=jenkins-controller \
  --serviceaccount=jenkins:jenkins --dry-run=client -o yaml | "${K[@]}" apply -f - >/dev/null
"${K[@]}" label namespace netci-build pod-security.kubernetes.io/enforce=baseline --overwrite >/dev/null
"${K[@]}" -n jenkins create secret generic jenkins-casc-secrets \
  --from-file=admin-password="${C}/jenkins_admin_password" \
  --from-file=cosign-key="${ROOT}/.netci-gate/jenkins/secrets/NETCI_COSIGN_PRIVATE_KEY" \
  --from-file=gitlab-token="${C}/gitlab_jenkins_token" \
  --from-file=harbor-robot-name="${C}/harbor_robot_name" \
  --from-file=harbor-robot-secret="${C}/harbor_robot_secret" \
  --from-file=netci-sa-password="${C}/jenkins_netci_sa_password" \
  --dry-run=client -o yaml | "${K[@]}" apply -f - >/dev/null
helm --kube-context "${CONTEXT}" upgrade --install jenkins jenkins/jenkins --version "${CHART_VERSION}" \
  -n jenkins -f "${ROOT}/infra/corp/jenkins/values.yaml" --wait --timeout 10m >/dev/null

log "up. scripts/corp/status.sh shows the state; scripts/corp/jenkins_failover.sh runs the drill."
