#!/usr/bin/env bash
# ingress-nginx for the corp lab, from the lab Harbor over verified TLS (ADR-031 manifest).
#
#   scripts/corp/ingress.sh
#
# The cluster has no route to registry.k8s.io, so the upstream images are copied into Harbor's
# mirror project and the manifest names them there by the digest Harbor returns: the pin
# survives the move.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CONTEXT=kind-netci-corp
NODES="netci-corp-worker netci-corp-worker3"
VIP=172.17.255.200
VERSION=v1.12.1
CERTGEN_VERSION=v1.5.2
SRC="${ROOT}/infra/lab-ingress/ingress-nginx-kind.yaml"
OUT="$(mktemp)"; trap 'rm -f "${OUT}"' EXIT
log() { printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*"; }

C="${ROOT}/.netci-gate/corp"
harbor_digest() {  # <repo> <tag> -> the digest Harbor serves for it, or nothing
  curl -s -I --cacert "${C}/pki/ca.crt" -u "$(<"${C}/harbor_ops_robot_name"):$(<"${C}/harbor_ops_robot_secret")" \
    -H 'Accept: application/vnd.oci.image.index.v1+json,application/vnd.oci.image.manifest.v1+json,application/vnd.docker.distribution.manifest.v2+json,application/vnd.docker.distribution.manifest.list.v2+json' \
    "https://172.17.0.1:8930/v2/mirror/ingress-nginx/$1/manifests/$2" | tr -d '\r' | awk -F': ' 'tolower($1)=="docker-content-digest"{print $2}'
}
mirror() {  # <repo> <tag> -> prints 172.17.0.1:8930/mirror/ingress-nginx/<repo>@<digest>
  local upstream="registry.k8s.io/ingress-nginx/$1:$2" target="172.17.0.1:8930/mirror/ingress-nginx/$1:$2" digest
  # Mirror tags are immutable in Harbor: once there, the stored digest is the pin.
  digest="$(harbor_digest "$1" "$2")"
  if [[ -z "${digest}" ]]; then
    docker pull -q "${upstream}" >/dev/null
    digest="$("${ROOT}/scripts/corp/push_image.sh" "${upstream}" "${target}")"
  fi
  [[ "${digest}" == sha256:* ]] || { echo "push of $1:$2 returned no digest" >&2; exit 1; }
  echo "172.17.0.1:8930/mirror/ingress-nginx/$1@${digest}"
}
CONTROLLER="$(mirror controller "${VERSION}")"
CERTGEN="$(mirror kube-webhook-certgen "${CERTGEN_VERSION}")"
log "controller ${CONTROLLER}"
sed -e "s#registry.k8s.io/ingress-nginx/controller:${VERSION}@sha256:[0-9a-f]*#${CONTROLLER}#" \
    -e "s#registry.k8s.io/ingress-nginx/kube-webhook-certgen:${CERTGEN_VERSION}@sha256:[0-9a-f]*#${CERTGEN}#" \
    "${SRC}" > "${OUT}"
grep -q "${CONTROLLER}" "${OUT}" && grep -q "${CERTGEN}" "${OUT}"
for node in ${NODES}; do kubectl --context "${CONTEXT}" label node "${node}" ingress-ready=true --overwrite >/dev/null; done
kubectl --context "${CONTEXT}" apply -f "${OUT}" >/dev/null
# Two replicas on two nodes behind one virtual address (MetalLB, scripts/corp/metallb.sh):
# losing a node -- the failover drill powers off worker3 -- does not take the entry point away.
kubectl --context "${CONTEXT}" -n ingress-nginx patch deploy ingress-nginx-controller --type merge -p '{"spec":{"replicas":2,"template":{"spec":{"affinity":{"podAntiAffinity":{"requiredDuringSchedulingIgnoredDuringExecution":[{"topologyKey":"kubernetes.io/hostname","labelSelector":{"matchLabels":{"app.kubernetes.io/component":"controller","app.kubernetes.io/instance":"ingress-nginx","app.kubernetes.io/name":"ingress-nginx"}}}]}}}}}}' >/dev/null
kubectl --context "${CONTEXT}" -n ingress-nginx annotate svc ingress-nginx-controller metallb.io/loadBalancerIPs="${VIP}" --overwrite >/dev/null
kubectl --context "${CONTEXT}" -n ingress-nginx patch svc ingress-nginx-controller --type merge -p '{"spec":{"type":"LoadBalancer"}}' >/dev/null
kubectl --context "${CONTEXT}" -n ingress-nginx rollout status deploy/ingress-nginx-controller --timeout=300s
kubectl --context "${CONTEXT}" -n ingress-nginx wait --for=condition=complete job/ingress-nginx-admission-patch --timeout=180s >/dev/null
for _ in $(seq 30); do
  [[ "$(kubectl --context "${CONTEXT}" -n ingress-nginx get svc ingress-nginx-controller -o jsonpath='{.status.loadBalancer.ingress[0].ip}')" == "${VIP}" ]] && break; sleep 2
done
log "ingress-nginx ${VERSION}: 2 replicas on ${NODES}, virtual address ${VIP}"
