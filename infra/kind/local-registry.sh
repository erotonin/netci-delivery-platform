#!/usr/bin/env bash
set -euo pipefail

# Connect an OCI registry to the kind cluster so nodes can pull the exact digest CI
# published. Two topologies are supported:
#
#   attach  (default) the registry runs as a container; it is joined to the kind
#           network and nodes address it by container name.
#   gateway the registry runs on the host network (needed where the Docker daemon has
#           no bridge NAT, so published ports do not work); nodes reach it through the
#           kind bridge gateway, while the host pushes to localhost.
#
# In both cases containerd's certs.d maps the reference host used in image names to the
# endpoint that node can actually reach, so the image reference -- and therefore the
# digest -- is identical on both sides.

CLUSTER_NAME="${CLUSTER_NAME:-netci-local}"
REGISTRY_NAME="${REGISTRY_NAME:-netci-registry}"
REGISTRY_PORT="${REGISTRY_PORT:-5000}"
REGISTRY_MODE="${REGISTRY_MODE:-attach}"
REGISTRY_PULL_HOST="${REGISTRY_PULL_HOST:-localhost:${REGISTRY_PORT}}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if ! docker network inspect kind >/dev/null 2>&1; then
  echo "kind network does not exist; create the kind cluster first" >&2
  exit 1
fi

case "${REGISTRY_MODE}" in
  attach)
    if ! docker ps --format '{{.Names}}' | grep -Fxq "${REGISTRY_NAME}"; then
      echo "registry container ${REGISTRY_NAME} is not running; start docker compose first" >&2
      exit 1
    fi
    if [[ "$(docker inspect -f '{{json .NetworkSettings.Networks.kind}}' "${REGISTRY_NAME}")" == "null" ]]; then
      docker network connect kind "${REGISTRY_NAME}"
    fi
    registry_endpoint="http://${REGISTRY_NAME}:${REGISTRY_PORT}"
    push_host="${REGISTRY_NAME}:${REGISTRY_PORT}"
    ;;
  gateway)
    gateway="$(docker network inspect kind --format '{{json .IPAM.Config}}' \
      | python3 -c 'import json,sys; print(next(c["Gateway"] for c in json.load(sys.stdin) if c.get("Gateway") and ":" not in c["Gateway"]))')"
    if [[ -z "${gateway}" ]]; then
      echo "could not determine the kind network IPv4 gateway" >&2
      exit 1
    fi
    registry_endpoint="http://${gateway}:${REGISTRY_PORT}"
    push_host="${REGISTRY_PULL_HOST}"
    ;;
  *)
    echo "REGISTRY_MODE must be attach or gateway" >&2
    exit 1
    ;;
esac

# Jenkins runs outside Kubernetes. Attach each running controller to kind so it
# can address the API server by the control-plane container name and agents can
# reach the controller by its compose DNS alias.
for service in jenkins-a jenkins-b; do
  controller="$(docker ps --filter "label=com.docker.compose.service=${service}" --format '{{.Names}}' | head -n 1)"
  if [[ -n "${controller}" ]] \
      && [[ "$(docker inspect -f '{{json .NetworkSettings.Networks.kind}}' "${controller}")" == "null" ]]; then
    docker network connect --alias "${service}" kind "${controller}"
  fi
done

for node in $(kind get nodes --name "${CLUSTER_NAME}"); do
  registry_dir="/etc/containerd/certs.d/${REGISTRY_PULL_HOST}"
  docker exec "${node}" mkdir -p "${registry_dir}"
  cat <<EOF | docker exec -i "${node}" sh -c "cat > '${registry_dir}/hosts.toml'"
[host."${registry_endpoint}"]
  capabilities = ["pull", "resolve", "push"]
EOF
done

kubectl apply -f "${SCRIPT_DIR}/namespaces.yaml"
kubectl apply -f "${SCRIPT_DIR}/jenkins-agent-rbac.yaml"

cat <<EOF | kubectl apply -f -
apiVersion: v1
kind: ConfigMap
metadata:
  name: local-registry-hosting
  namespace: kube-public
data:
  localRegistryHosting.v1: |
    host: "${REGISTRY_PULL_HOST}"
    pushHost: "${push_host}"
    help: "https://kind.sigs.k8s.io/docs/user/local-registry/"
EOF

echo "registry mode=${REGISTRY_MODE} endpoint=${registry_endpoint} reference-host=${REGISTRY_PULL_HOST} connected to kind cluster ${CLUSTER_NAME}"
echo "export KUBERNETES_SERVICE_ACCOUNT_TOKEN=\$(kubectl -n netci-build create token jenkins-controller --duration=24h)"
