#!/usr/bin/env bash
# Install ingress-nginx on the lab kind cluster from the lab registry (ADR-031).
#
# The cluster is offline, so the upstream images are mirrored into the lab registry
# first and the manifest is rewritten to name them there -- by the digest the mirror
# returns, so the pin survives the move. The controller listens on the worker node's
# ports 80/443 (kind's hostPort provider); the node is reachable from the host at its
# docker-network address, which is what the canary proof samples.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
VERSION="${INGRESS_NGINX_VERSION:-v1.12.1}"
CERTGEN_VERSION="${INGRESS_NGINX_CERTGEN_VERSION:-v1.5.2}"
PUSH_HOST="${NETCI_LAB_REGISTRY_PUSH:-localhost:55000}"
PULL_HOST="${NETCI_LAB_REGISTRY_PULL:-172.17.0.1:55000}"
CLUSTER="${NETCI_LAB_KIND_CLUSTER:-netci-local}"
NODE="${NETCI_LAB_INGRESS_NODE:-${CLUSTER}-worker}"
SRC="$ROOT/infra/lab-ingress/ingress-nginx-kind.yaml"
OUT="$(mktemp)"
trap 'rm -f "$OUT"' EXIT

mirror() {  # <upstream repo> <tag> -> prints <pull host>/<repo>@<digest>
  local repo="$1" tag="$2" digest
  docker pull -q "registry.k8s.io/ingress-nginx/$repo:$tag" >/dev/null
  docker tag "registry.k8s.io/ingress-nginx/$repo:$tag" "$PUSH_HOST/ingress-nginx/$repo:$tag"
  digest="$(docker push "$PUSH_HOST/ingress-nginx/$repo:$tag" | sed -n 's/.*digest: \(sha256:[0-9a-f]*\).*/\1/p' | tail -1)"
  [ -n "$digest" ] || { echo "push of $repo:$tag returned no digest" >&2; exit 1; }
  echo "$PULL_HOST/ingress-nginx/$repo@$digest"
}

CONTROLLER="$(mirror controller "$VERSION")"
CERTGEN="$(mirror kube-webhook-certgen "$CERTGEN_VERSION")"
sed -e "s#registry.k8s.io/ingress-nginx/controller:${VERSION}@sha256:[0-9a-f]*#${CONTROLLER}#" \
    -e "s#registry.k8s.io/ingress-nginx/kube-webhook-certgen:${CERTGEN_VERSION}@sha256:[0-9a-f]*#${CERTGEN}#" \
    "$SRC" > "$OUT"
grep -q "$CONTROLLER" "$OUT" && grep -q "$CERTGEN" "$OUT"

kubectl --context "kind-$CLUSTER" label node "$NODE" ingress-ready=true --overwrite >/dev/null
kubectl --context "kind-$CLUSTER" apply -f "$OUT"
kubectl --context "kind-$CLUSTER" -n ingress-nginx rollout status deploy/ingress-nginx-controller --timeout=180s
kubectl --context "kind-$CLUSTER" -n ingress-nginx wait --for=condition=complete job/ingress-nginx-admission-patch --timeout=120s
echo "ingress-nginx $VERSION ready on $NODE ($(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "$NODE"))"
