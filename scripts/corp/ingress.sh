#!/usr/bin/env bash
# ingress-nginx for the corp lab, from the lab Harbor over verified TLS (ADR-031 manifest).
#
#   scripts/corp/ingress.sh
#
# The cluster has no route to registry.k8s.io, so the upstream images are copied into Harbor's
# mirror project and the manifest names them there by the digest Harbor returns: the pin
# survives the move. The controller listens on ports 80/443 of netci-corp-worker (kind's
# hostPort provider), a node the Jenkins failover drill never powers off.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CONTEXT=kind-netci-corp
NODE=netci-corp-worker
VERSION=v1.12.1
CERTGEN_VERSION=v1.5.2
SRC="${ROOT}/infra/lab-ingress/ingress-nginx-kind.yaml"
OUT="$(mktemp)"; trap 'rm -f "${OUT}"' EXIT
log() { printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*"; }

mirror() {  # <repo> <tag> -> prints 172.17.0.1:8930/mirror/ingress-nginx/<repo>@<digest>
  local upstream="registry.k8s.io/ingress-nginx/$1:$2" target="172.17.0.1:8930/mirror/ingress-nginx/$1:$2" digest
  docker pull -q "${upstream}" >/dev/null
  digest="$("${ROOT}/scripts/corp/push_image.sh" "${upstream}" "${target}")"
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
kubectl --context "${CONTEXT}" label node "${NODE}" ingress-ready=true --overwrite >/dev/null
kubectl --context "${CONTEXT}" apply -f "${OUT}" >/dev/null
kubectl --context "${CONTEXT}" -n ingress-nginx rollout status deploy/ingress-nginx-controller --timeout=300s
kubectl --context "${CONTEXT}" -n ingress-nginx wait --for=condition=complete job/ingress-nginx-admission-patch --timeout=180s >/dev/null
log "ingress-nginx ${VERSION} ready on ${NODE} ($(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "${NODE}"))"
