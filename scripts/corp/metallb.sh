#!/usr/bin/env bash
# MetalLB (L2) on the corp lab: the virtual address in front of netCI's ingress replicas.
#
#   scripts/corp/metallb.sh
#
# Manifest vendored and pinned (infra/corp/metallb/metallb-native.yaml, v0.14.9); its images
# are copied into Harbor's mirror and named there by digest, as scripts/corp/ingress.sh does.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
C="${ROOT}/.netci-gate/corp"
CONTEXT=kind-netci-corp
VERSION=v0.14.9
SRC="${ROOT}/infra/corp/metallb/metallb-native.yaml"
OUT="$(mktemp)"; trap 'rm -f "${OUT}"' EXIT
log() { printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*"; }

harbor_digest() {  # <repo> <tag> -> the digest Harbor serves for mirror/metallb/<repo>, or nothing
  curl -s -I --cacert "${C}/pki/ca.crt" -u "$(<"${C}/harbor_ops_robot_name"):$(<"${C}/harbor_ops_robot_secret")" \
    -H 'Accept: application/vnd.oci.image.index.v1+json,application/vnd.oci.image.manifest.v1+json,application/vnd.docker.distribution.manifest.v2+json,application/vnd.docker.distribution.manifest.list.v2+json' \
    "https://172.17.0.1:8930/v2/mirror/metallb/$1/manifests/$2" | tr -d '\r' | awk -F': ' 'tolower($1)=="docker-content-digest"{print $2}'
}
mirror() {  # <repo> -> prints 172.17.0.1:8930/mirror/metallb/<repo>@<digest>
  local digest
  digest="$(harbor_digest "$1" "${VERSION}")"   # mirror tags are immutable: reuse the stored digest
  if [[ -z "${digest}" ]]; then
    docker pull -q "quay.io/metallb/$1:${VERSION}" >/dev/null
    digest="$("${ROOT}/scripts/corp/push_image.sh" "quay.io/metallb/$1:${VERSION}" "172.17.0.1:8930/mirror/metallb/$1:${VERSION}")"
  fi
  [[ "${digest}" == sha256:* ]] || { echo "no digest for $1" >&2; exit 1; }
  echo "172.17.0.1:8930/mirror/metallb/$1@${digest}"
}
CONTROLLER="$(mirror controller)"
SPEAKER="$(mirror speaker)"
sed -e "s#quay.io/metallb/controller:${VERSION}#${CONTROLLER}#" -e "s#quay.io/metallb/speaker:${VERSION}#${SPEAKER}#" "${SRC}" > "${OUT}"
grep -q "${CONTROLLER}" "${OUT}" && grep -q "${SPEAKER}" "${OUT}"
kubectl --context "${CONTEXT}" apply -f "${OUT}" >/dev/null
kubectl --context "${CONTEXT}" -n metallb-system rollout status deploy/controller --timeout=300s >/dev/null
kubectl --context "${CONTEXT}" -n metallb-system rollout status ds/speaker --timeout=300s >/dev/null
# The pool's CRDs are served by the controller's webhook, which needs a moment after rollout.
for _ in $(seq 30); do kubectl --context "${CONTEXT}" apply -f "${ROOT}/infra/corp/metallb/pool.yaml" >/dev/null 2>&1 && break; sleep 3; done
kubectl --context "${CONTEXT}" -n metallb-system get ipaddresspool netci-ingress >/dev/null
log "MetalLB ${VERSION} ready; pool netci-ingress = 172.17.255.200"
