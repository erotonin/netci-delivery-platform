#!/usr/bin/env bash
# Bring up the two Jenkins controllers against the kind build cluster.
#
# Compose is the documented topology, but it needs published ports. Where the Docker
# daemon has no bridge NAT, the controllers instead join the kind network directly at
# fixed addresses, which also happens to be the cleanest route for the three parties
# that must reach each other:
#
#   host    -> controller     for the netCI adapter and the gate runners
#   controller -> kube API    at https://netci-local-control-plane:6443 (a cert SAN)
#   agent pod  -> controller  by the controller's address on the kind network
#
#   scripts/jenkins_lab.sh up        build images, mirror agent images, start A and B
#   scripts/jenkins_lab.sh recreate <a|b>   destroy and rebuild one controller from JCasC
#   scripts/jenkins_lab.sh status
#   scripts/jenkins_lab.sh down
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CLUSTER="${NETCI_KIND_CLUSTER:-netci-local}"
NETWORK="${NETCI_JENKINS_NETWORK:-kind}"
CONTROLLER_IMAGE="${NETCI_JENKINS_IMAGE:-netci/jenkins-controller:0.1.0}"
TOOLBOX_IMAGE="${JENKINS_TOOLBOX_IMAGE:-netci/ci-toolbox:0.4.0}"
INBOUND_AGENT_IMAGE="${JENKINS_INBOUND_AGENT_IMAGE:-jenkins/inbound-agent:3386.v353e57a_1b_ea_0-1-jdk21}"
REGISTRY="${NETCI_LAB_REGISTRY:-localhost:55000}"
REGISTRY_HOST_PORT="${NETCI_LAB_REGISTRY_PORT:-55000}"
ADMIN_PASSWORD="${JENKINS_ADMIN_PASSWORD:-change-me-local-only}"
NETCI_PIPELINE_API_KEY="${NETCI_PIPELINE_API_KEY:-netci-local-pipeline-key}"
NETCI_API_PORT="${NETCI_LAB_API_PORT:-8100}"
GIT_SERVER_IMAGE="${NETCI_GIT_SERVER_IMAGE:-python:3.12-alpine}"
GOLDEN_BASE_IMAGE="${NETCI_GOLDEN_BASE_IMAGE:-netci/python-base:3.12-alpine}"
TRIVY_DB_IMAGE="${NETCI_TRIVY_DB_IMAGE:-aquasec/trivy-db:2}"

# Fixed addresses so the agent callback URL is known before the container starts.
declare -A CONTROLLER_IP=([a]="172.17.0.50" [b]="172.17.0.51")
GIT_SERVER_IP="172.17.0.52"
declare -A SHARED_AGENT_IP=([a]="172.17.0.60" [b]="172.17.0.61")
# The shared agent exists only for the benchmark baseline; the acceptance topology
# deliberately has no agent that outlives a build.
WITH_SHARED_AGENT="${NETCI_WITH_SHARED_AGENT:-1}"

log() { printf '  %s\n' "$*"; }

casc_files() {
  local letter="$1"
  local files="/var/jenkins_home/casc/base.yaml,/var/jenkins_home/casc/controller-${letter}.yaml,/var/jenkins_home/casc/ephemeral-agent.yaml"
  if [[ "${WITH_SHARED_AGENT}" == "1" ]]; then
    files="${files},/var/jenkins_home/casc/shared-agent.yaml"
  fi
  printf '%s' "${files}"
}

kind_gateway() {
  docker network inspect "${NETWORK}" --format '{{json .IPAM.Config}}' \
    | python3 -c 'import json,sys; print(next(c["Gateway"] for c in json.load(sys.stdin) if c.get("Gateway") and ":" not in c["Gateway"]))'
}

require_cluster() {
  if ! kind get clusters 2>/dev/null | grep -Fxq "${CLUSTER}"; then
    echo "kind cluster ${CLUSTER} is not running; run 'make kind-up' first" >&2
    exit 1
  fi
}

mirror_image() {
  # The build cluster has no route to Docker Hub, so every image an agent pod needs
  # must exist in the local registry first.
  local source="$1" target="${REGISTRY}/$1"
  if curl -sf "http://127.0.0.1:${REGISTRY_HOST_PORT}/v2/${source%%:*}/tags/list" >/dev/null 2>&1; then
    log "already mirrored: ${target}"
    return 0
  fi
  docker image inspect "${source}" >/dev/null 2>&1 || docker pull -q "${source}" >/dev/null
  docker tag "${source}" "${target}"
  docker push -q "${target}" >/dev/null
  log "mirrored ${target}"
}

build_images() {
  log "building the agent toolbox"
  docker build --network host --quiet --tag "${TOOLBOX_IMAGE}" "${ROOT}/jenkins/agent-toolbox" >/dev/null
  log "building the controller"
  docker build --network host --quiet --tag "${CONTROLLER_IMAGE}" \
    -f "${ROOT}/jenkins/Dockerfile.controller" "${ROOT}/jenkins" >/dev/null
}

publish_agent_images() {
  mirror_image "${INBOUND_AGENT_IMAGE}"
  # The golden base the sample applications build on. It is built here rather than
  # pulled, because patching it is the point (see sample-apps/base-python/Dockerfile).
  docker build --network host --quiet --tag "${GOLDEN_BASE_IMAGE}" \
    "${ROOT}/sample-apps/base-python" >/dev/null
  docker tag "${GOLDEN_BASE_IMAGE}" "${REGISTRY}/${GOLDEN_BASE_IMAGE}"
  docker push -q "${REGISTRY}/${GOLDEN_BASE_IMAGE}" >/dev/null
  log "pushed ${REGISTRY}/${GOLDEN_BASE_IMAGE}"
  # Trivy's vulnerability database, so the scan works without internet access.
  mirror_image "${TRIVY_DB_IMAGE}"
  docker tag "${TOOLBOX_IMAGE}" "${REGISTRY}/${TOOLBOX_IMAGE}"
  docker push -q "${REGISTRY}/${TOOLBOX_IMAGE}" >/dev/null
  log "pushed ${REGISTRY}/${TOOLBOX_IMAGE}"
}

start_git_server() {
  # Jenkins checks out over Git, so the agent pod needs a reachable remote.
  #
  # Served over HTTP rather than git://, because netCI validates repositoryUrl as an
  # http(s) URL -- the right contract for a portal, and not something to relax so a lab
  # can use a simpler protocol. A bare repo plus `git update-server-info` is enough for
  # a read-only clone.
  #
  # The snapshot is taken from the *working tree*, not from HEAD: a gate that builds the
  # last commit would silently test code you are no longer running. Nothing in the
  # developer's repository is modified -- the files are copied into a scratch repo.
  docker rm -f -v netci-git-server >/dev/null 2>&1 || true
  local work="${ROOT}/.netci-gate/git/worktree"
  local mirror="${ROOT}/.netci-gate/git/netci.git"
  rm -rf "${ROOT}/.netci-gate/git"
  mkdir -p "${work}"

  # Tracked plus untracked-but-not-ignored: exactly what a commit would contain.
  # `git archive` only sees committed content, so the file list comes from ls-files.
  ( cd "${ROOT}" && git ls-files -co --exclude-standard -z \
      | tar --null --files-from=- --create --file - ) \
    | tar --extract --file - --directory "${work}"

  git -C "${work}" init --quiet --initial-branch=main
  git -C "${work}" add -A
  git -C "${work}" -c user.name='netCI lab' -c user.email='netci@localhost' \
    commit --quiet -m "netCI lab snapshot of the working tree"
  git clone --quiet --bare "${work}" "${mirror}"
  git -C "${mirror}" update-server-info
  touch "${mirror}/git-daemon-export-ok"

  # The shared library is published as its own repository, the way it would be in a real
  # deployment. Jenkins' git plugin refuses a file:// remote as an insecure local
  # checkout, so the in-image snapshot cannot be used without disabling that guard.
  local library="${ROOT}/.netci-gate/git/netci-shared-library.git"
  local library_work="${work}-library"
  rm -rf "${library_work}"
  cp -r "${ROOT}/jenkins/shared-library" "${library_work}"
  git -C "${library_work}" init --quiet --initial-branch=main
  git -C "${library_work}" add -A
  git -C "${library_work}" -c user.name='netCI lab' -c user.email='netci@localhost' \
    commit --quiet -m "netCI shared library snapshot"
  git clone --quiet --bare "${library_work}" "${library}"
  git -C "${library}" update-server-info
  touch "${library}/git-daemon-export-ok"

  chmod -R a+rX "${ROOT}/.netci-gate/git"

  docker run -d --name netci-git-server --network "${NETWORK}" --ip "${GIT_SERVER_IP}" \
    -v "${ROOT}/.netci-gate/git:/srv/git:ro" -w /srv/git \
    --entrypoint python3 "${GIT_SERVER_IMAGE}" -m http.server 80 --bind 0.0.0.0 >/dev/null
  for _ in $(seq 1 30); do
    if curl -sf "http://${GIT_SERVER_IP}/netci.git/info/refs" >/dev/null 2>&1; then
      log "git served at http://${GIT_SERVER_IP}/netci.git ($(git -C "${mirror}" rev-parse --short HEAD))"
      log "shared library at http://${GIT_SERVER_IP}/netci-shared-library.git"
      return 0
    fi
    sleep 1
  done
  echo "git server did not come up" >&2
  return 1
}

cosign_key() {
  # A throwaway signing key for the lab. A real deployment injects one from a secret
  # manager; the point here is that the key reaches builds as a Jenkins credential
  # rather than as a file baked into an image.
  local key_dir="${ROOT}/.netci-gate/keys"
  mkdir -p "${key_dir}"
  if [[ ! -f "${key_dir}/cosign.key" ]]; then
    ( cd "${key_dir}" && COSIGN_PASSWORD="" cosign generate-key-pair --output-key-prefix cosign >/dev/null )
  fi
  NETCI_COSIGN_PRIVATE_KEY="$(cat "${key_dir}/cosign.key")"
  export NETCI_COSIGN_PRIVATE_KEY
}

kube_credentials() {
  KUBERNETES_SERVICE_ACCOUNT_TOKEN="$(kubectl -n netci-build create token jenkins-controller --duration=24h)"
  KUBERNETES_SERVER_CA="$(kubectl config view --raw --minify --flatten \
    -o jsonpath='{.clusters[0].cluster.certificate-authority-data}' | base64 -d)"
  export KUBERNETES_SERVICE_ACCOUNT_TOKEN KUBERNETES_SERVER_CA
}

start_controller() {
  local letter="$1" ip="${CONTROLLER_IP[$1]}" name="jenkins-${1}"
  # -v matters: the Jenkins image declares VOLUME /var/jenkins_home, so each recreate
  # leaves an anonymous ~370MB volume behind unless it is removed with the container.
  # The rebuild gate recreates controllers repeatedly, so this leaks fast.
  docker rm -f -v "${name}" >/dev/null 2>&1 || true
  docker run -d --name "${name}" \
    --network "${NETWORK}" --ip "${ip}" --network-alias "${name}" \
    -v "${ROOT}/jenkins/casc:/var/jenkins_home/casc:ro" \
    -e CASC_JENKINS_CONFIG="$(casc_files "${letter}")" \
    -e JENKINS_CONTROLLER_ID="${name}" \
    -e JENKINS_ADMIN_PASSWORD="${ADMIN_PASSWORD}" \
    -e NETCI_PIPELINE_API_KEY="${NETCI_PIPELINE_API_KEY}" \
    -e NETCI_COSIGN_PRIVATE_KEY="${NETCI_COSIGN_PRIVATE_KEY}" \
    -e KUBERNETES_API_URL="https://${CLUSTER}-control-plane:6443" \
    -e KUBERNETES_SERVER_CA="${KUBERNETES_SERVER_CA}" \
    -e KUBERNETES_SERVICE_ACCOUNT_TOKEN="${KUBERNETES_SERVICE_ACCOUNT_TOKEN}" \
    -e KUBERNETES_CREDENTIALS_ID=netci-kind-token \
    -e JENKINS_AGENT_URL="http://${ip}:8080" \
    -e JENKINS_SHARED_LIBRARY_REMOTE="http://${GIT_SERVER_IP}/netci-shared-library.git" \
    -e JENKINS_INBOUND_AGENT_IMAGE="${REGISTRY}/${INBOUND_AGENT_IMAGE}" \
    -e JENKINS_TOOLBOX_IMAGE="${REGISTRY}/${TOOLBOX_IMAGE}" \
    -e JAVA_OPTS="-Djenkins.install.runSetupWizard=false" \
    "${CONTROLLER_IMAGE}" >/dev/null
  log "started ${name} at http://${ip}:8080"
}

start_shared_agent() {
  # A long-lived agent that keeps its workspace between builds. It is the baseline the
  # benchmark measures the ephemeral pod against, and nothing else should use it.
  local letter="$1" name="netci-shared-agent-${1}" controller="${CONTROLLER_IP[$1]}"
  local secret
  secret="$(curl -sf -u "admin:${ADMIN_PASSWORD}" \
    "http://${controller}:8080/computer/netci-shared/jenkins-agent.jnlp" \
    | sed -n 's:.*<argument>\([a-f0-9]\{64\}\)</argument>.*:\1:p' | head -n 1)"
  if [[ -z "${secret}" ]]; then
    log "no shared-agent secret from jenkins-${letter}; skipping the baseline agent"
    return 0
  fi
  docker rm -f -v "${name}" >/dev/null 2>&1 || true
  # `seccomp=unconfined` is what a long-lived agent costs: Docker's default profile blocks
  # clone(CLONE_NEWUSER), so rootless buildah cannot run without it. The ephemeral
  # Kubernetes agent needs no such grant -- the kubelet's RuntimeDefault profile already
  # permits it, and the pod is destroyed after the build either way. That asymmetry is one
  # of the things `scripts/gate_benchmark.py` is measuring, so it is deliberate here.
  docker run -d --name "${name}" --network "${NETWORK}" --ip "${SHARED_AGENT_IP[$1]}" \
    --security-opt seccomp=unconfined --security-opt apparmor=unconfined \
    --entrypoint java "${TOOLBOX_IMAGE}" \
    -jar /usr/share/jenkins/agent.jar \
    -url "http://${controller}:8080/" -secret "${secret}" -name netci-shared \
    -workDir /home/jenkins/agent >/dev/null
  log "shared agent attached to jenkins-${letter}"
}

wait_for_controller() {
  local ip="$1" name="$2"
  for _ in $(seq 1 120); do
    if curl -sf --max-time 3 -u "admin:${ADMIN_PASSWORD}" "http://${ip}:8080/api/json?tree=mode" >/dev/null 2>&1; then
      log "${name}: ready"
      return 0
    fi
    sleep 2
  done
  echo "${name} did not become ready; last logs:" >&2
  docker logs --tail 40 "${name}" >&2
  return 1
}

up() {
  require_cluster
  build_images
  publish_agent_images
  start_git_server
  cosign_key
  kube_credentials
  for letter in a b; do
    start_controller "${letter}"
  done
  for letter in a b; do
    wait_for_controller "${CONTROLLER_IP[$letter]}" "jenkins-${letter}"
    [[ "${WITH_SHARED_AGENT}" == "1" ]] && start_shared_agent "${letter}"
  done
  summary
}

recreate() {
  local letter="${1:?usage: jenkins_lab.sh recreate <a|b>}"
  require_cluster
  cosign_key
  kube_credentials
  start_controller "${letter}"
  wait_for_controller "${CONTROLLER_IP[$letter]}" "jenkins-${letter}"
}

summary() {
  cat <<EOF

Jenkins controllers are up. Point netCI at them:

  export NETCI_CI_MODE=jenkins
  export NETCI_JENKINS_CONTROLLERS=A,B
  export JENKINS_A_URL=http://${CONTROLLER_IP[a]}:8080 JENKINS_A_USERNAME=admin JENKINS_A_API_TOKEN='${ADMIN_PASSWORD}'
  export JENKINS_B_URL=http://${CONTROLLER_IP[b]}:8080 JENKINS_B_USERNAME=admin JENKINS_B_API_TOKEN='${ADMIN_PASSWORD}'
  export NETCI_CALLBACK_URL=http://$(kind_gateway):${NETCI_API_PORT}
  # The build agent reaches the registry through the kind gateway, not through
  # localhost -- inside a pod, localhost is the pod.
  export NETCI_REGISTRY_PUSH_HOST=$(kind_gateway):${REGISTRY_HOST_PORT}
  export NETCI_REGISTRY_PULL_HOST=$(kind_gateway):${REGISTRY_HOST_PORT}
  export NETCI_BUILD_BASE_IMAGE=$(kind_gateway):${REGISTRY_HOST_PORT}/${GOLDEN_BASE_IMAGE}
  export NETCI_TRIVY_DB_REPOSITORY=$(kind_gateway):${REGISTRY_HOST_PORT}/${TRIVY_DB_IMAGE}
  export NETCI_GIT_URL=http://${GIT_SERVER_IP}/netci.git

  make jenkins-ci-loop
  make jenkins-rebuild
  make failure-drill
  make benchmark
EOF
}

status() {
  docker ps --filter name=jenkins- --filter name=netci-git-server \
    --format 'table {{.Names}}\t{{.Status}}\t{{.Networks}}'
  for letter in a b; do
    printf 'jenkins-%s health: ' "${letter}"
    curl -sf --max-time 3 -u "admin:${ADMIN_PASSWORD}" \
      "http://${CONTROLLER_IP[$letter]}:8080/api/json?tree=mode" >/dev/null 2>&1 \
      && echo reachable || echo unreachable
  done
}

down() {
  docker rm -f -v jenkins-a jenkins-b netci-git-server \
    netci-shared-agent-a netci-shared-agent-b >/dev/null 2>&1 || true
  echo "Jenkins controllers, shared agents and git server stopped"
}

publish() {
  # Re-publish the working tree and the shared library, and make the running controllers
  # re-read jenkins/casc. This is the edit-and-retry loop: changing a pipeline stage or an
  # agent template otherwise needs a full `down`/`up`, which throws away ~4 minutes of
  # controller startup to pick up a one-line change.
  #
  # It cannot pick up a change to the controller *image* or to the environment the
  # container was started with -- use `recreate <a|b>` for those.
  start_git_server
  for letter in a b; do
    local ip="${CONTROLLER_IP[$letter]}"
    # Authenticated: anonymous read is off, so an unauthenticated GET / answers 403 and
    # would report a healthy controller as down.
    curl -sf --max-time 5 -u "admin:${ADMIN_PASSWORD}" \
      "http://${ip}:8080/api/json?tree=mode" >/dev/null 2>&1 \
      || { log "jenkins-${letter} is not up; skipped"; continue; }
    local jar="${ROOT}/.netci-gate/jenkins/cookies-${letter}"
    local crumb
    crumb="$(curl -sf -c "${jar}" -u "admin:${ADMIN_PASSWORD}" \
      "http://${ip}:8080/crumbIssuer/api/json" | sed -n 's/.*"crumb":"\([^"]*\)".*/\1/p')"
    if curl -sf -o /dev/null -b "${jar}" -u "admin:${ADMIN_PASSWORD}" \
         -H "Jenkins-Crumb: ${crumb}" -X POST \
         "http://${ip}:8080/configuration-as-code/reload"; then
      log "jenkins-${letter} reloaded its JCasC"
    else
      log "jenkins-${letter} refused the JCasC reload; recreate it instead"
    fi
  done
  log "controllers re-clone the shared library on the next build"
}

case "${1:-up}" in
  up) up ;;
  publish) publish ;;
  recreate) recreate "${2:-}" ;;
  status) status ;;
  down) down ;;
  gateway) kind_gateway ;;
  *) echo "usage: scripts/jenkins_lab.sh {up|publish|recreate <a|b>|status|down|gateway}" >&2; exit 2 ;;
esac
