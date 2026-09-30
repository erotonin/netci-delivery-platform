#!/usr/bin/env bash
# k3s on the lab guests: three servers with embedded etcd, every node also schedulable -- the
# shape of a small company build/controller cluster. Then Longhorn, which replicates each
# volume synchronously to three nodes: a cell's JENKINS_HOME survives the loss of a node
# without a restore (ADR-060).
#
#   lab/k3s.sh up        install or finish installing (idempotent)
#   lab/k3s.sh kubeconfig  print the path of the host-side kubeconfig
#
# Versions are pinned here and nowhere else in the lab.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STATE="${ROOT}/.netci-gate/lab"
K3S_VERSION="${K3S_VERSION:-v1.36.4+k3s1}"
LONGHORN_VERSION="${LONGHORN_VERSION:-v1.13.0}"
CA="${ROOT}/.netci-gate/corp/pki/ca.crt"      # the lab CA Harbor's certificate chains to
HARBOR=172.17.0.1:8930
KUBECONFIG_OUT="${STATE}/kubeconfig"
VMS="${ROOT}/lab/vms.sh"
log() { printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*"; }
ip() { echo "192.168.122.$((210 + $1))"; }
on() { local i=$1; shift; "${VMS}" ssh "$i" "$@"; }

registries() {  # containerd on every node trusts the lab CA for Harbor; nothing else changes
  local i=$1
  on "$i" "sudo mkdir -p /etc/rancher/k3s /etc/netci && sudo tee /etc/netci/lab-ca.crt >/dev/null" < "${CA}"
  on "$i" "sudo tee /etc/rancher/k3s/registries.yaml >/dev/null" <<EOF
configs:
  "${HARBOR}":
    tls:
      ca_file: /etc/netci/lab-ca.crt
EOF
}

install_server() {
  local i=$1 extra=$2
  on "$i" 'test -x /usr/local/bin/k3s' && { log "k3s already on node $i"; return; }
  log "installing k3s ${K3S_VERSION} on node $i"
  # traefik and servicelb are left out: the cells' addresses are Services, and ingress is
  # added deliberately when the intake needs it. The token file never reaches a command line.
  on "$i" "sudo install -d -m 700 /etc/netci && sudo tee /etc/netci/k3s-token >/dev/null && sudo chmod 600 /etc/netci/k3s-token" < "${STATE}/k3s-token"
  on "$i" "curl -sfL https://get.k3s.io | sudo INSTALL_K3S_VERSION='${K3S_VERSION}' sh -s - server ${extra} \
    --token-file /etc/netci/k3s-token --node-ip $(ip "$i") \
    --tls-san $(ip 1) --tls-san $(ip 2) --tls-san $(ip 3) \
    --disable traefik --disable servicelb --write-kubeconfig-mode 600" >/dev/null
}

up() {
  [[ -s "${STATE}/k3s-token" ]] || ( umask 077; openssl rand -hex 32 > "${STATE}/k3s-token" )
  for i in 1 2 3; do registries "$i"; done
  install_server 1 "--cluster-init"
  for i in 2 3; do install_server "$i" "--server https://$(ip 1):6443"; done
  on 1 "sudo cat /etc/rancher/k3s/k3s.yaml" | sed "s#https://127.0.0.1:6443#https://$(ip 1):6443#" > "${KUBECONFIG_OUT}"
  chmod 600 "${KUBECONFIG_OUT}"
  export KUBECONFIG="${KUBECONFIG_OUT}"
  kubectl wait --for=condition=Ready node --all --timeout=300s >/dev/null
  log "k3s: $(kubectl get nodes --no-headers | wc -l) nodes Ready"

  if ! kubectl get ns longhorn-system >/dev/null 2>&1 || ! kubectl -n longhorn-system get deploy longhorn-driver-deployer >/dev/null 2>&1; then
    log "installing Longhorn ${LONGHORN_VERSION}"
    kubectl apply -f "https://raw.githubusercontent.com/longhorn/longhorn/${LONGHORN_VERSION}/deploy/longhorn.yaml" >/dev/null
  fi
  kubectl -n longhorn-system rollout status deploy/longhorn-driver-deployer --timeout=600s >/dev/null
  kubectl -n longhorn-system wait --for=condition=Ready pod -l app=longhorn-manager --timeout=600s >/dev/null
  log "Longhorn ready; storage classes: $(kubectl get sc -o name | tr '\n' ' ')"
}

case "${1:-}" in
  up) up ;;
  kubeconfig) echo "${KUBECONFIG_OUT}" ;;
  *) sed -n '2,11p' "$0" >&2; exit 2 ;;
esac
