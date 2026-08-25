#!/usr/bin/env bash
set -euo pipefail

CLUSTER_NAME="${CLUSTER_NAME:-netci-local}"
REGISTRY_NAME="${REGISTRY_NAME:-netci-registry}"
REGISTRY_PORT="${REGISTRY_PORT:-5000}"

if ! docker network inspect kind >/dev/null 2>&1; then
  echo "kind network does not exist; create the kind cluster first" >&2
  exit 1
fi

if ! docker ps --format '{{.Names}}' | grep -Fxq "${REGISTRY_NAME}"; then
  echo "registry container ${REGISTRY_NAME} is not running; start docker compose first" >&2
  exit 1
fi

docker network connect kind "${REGISTRY_NAME}" 2>/dev/null || true

for node in $(kind get nodes --name "${CLUSTER_NAME}"); do
  docker exec "${node}" mkdir -p "/etc/containerd/certs.d/${REGISTRY_NAME}:${REGISTRY_PORT}"
  cat <<EOF | docker exec -i "${node}" sh -c "cat > /etc/containerd/certs.d/${REGISTRY_NAME}:${REGISTRY_PORT}/hosts.toml"
server = "http://${REGISTRY_NAME}:${REGISTRY_PORT}"
[host."http://${REGISTRY_NAME}:${REGISTRY_PORT}"]
  capabilities = ["pull", "resolve", "push"]
EOF
done

kubectl create namespace netci-build --dry-run=client -o yaml | kubectl apply -f -
kubectl create namespace dev --dry-run=client -o yaml | kubectl apply -f -
kubectl create namespace staging --dry-run=client -o yaml | kubectl apply -f -
kubectl create namespace prod --dry-run=client -o yaml | kubectl apply -f -

echo "registry ${REGISTRY_NAME}:${REGISTRY_PORT} connected to kind cluster ${CLUSTER_NAME}"
