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
#   scripts/jenkins_lab.sh reload    make both controllers re-read jenkins/casc and /run/secrets
#   scripts/jenkins_lab.sh rotate-token   mint a fresh 24h kube service-account token, reload
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
# The toolbox has git and python3: enough for scripts/lab/git_smart_http.py, which
# serves the repositories over the smart protocol (see that file for why dumb HTTP
# was not acceptable once build time was being measured).
GIT_SERVER_IMAGE="${NETCI_GIT_SERVER_IMAGE:-${TOOLBOX_IMAGE:-netci/ci-toolbox:0.4.0}}"
GOLDEN_BASE_IMAGE="${NETCI_GOLDEN_BASE_IMAGE:-netci/python-base:3.12-alpine}"
TRIVY_DB_IMAGE="${NETCI_TRIVY_DB_IMAGE:-aquasec/trivy-db:2}"

# Fixed addresses so the agent callback URL is known before the container starts.
declare -A CONTROLLER_IP=([a]="172.17.0.50" [b]="172.17.0.51")
GIT_SERVER_IP="172.17.0.52"
declare -A SHARED_AGENT_IP=([a]="172.17.0.60" [b]="172.17.0.61")
# The shared agent exists only for the benchmark baseline; the acceptance topology
# deliberately has no agent that outlives a build.

log() { printf '  %s\n' "$*"; }

casc_files() {
  local letter="$1"
  local files="/var/jenkins_home/casc/base.yaml,/var/jenkins_home/casc/controller-${letter}.yaml,/var/jenkins_home/casc/ephemeral-agent.yaml"
  # The benchmark baseline is now a reusable pod template inside ephemeral-agent.yaml
  # (`netci-shared`); no separate long-lived JNLP container is needed.
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
  # can use a simpler protocol. Smart HTTP (git http-backend), not the dumb protocol:
  # a dumb fetch of an up-to-date repository cost ~6 s here and would have been read as
  # a cost of the build agent in the benchmark.
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
    -v "${ROOT}/.netci-gate/git:/srv/git:ro" -v "${ROOT}/scripts/lab/git_smart_http.py:/srv/git_smart_http.py:ro" \
    --user 0 --entrypoint python3 "${GIT_SERVER_IMAGE}" /srv/git_smart_http.py --root /srv/git --port 80 >/dev/null
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
  KUBERNETES_SERVER_CA="$(kubectl config view --raw --minify --flatten \
    -o jsonpath='{.clusters[0].cluster.certificate-authority-data}' | base64 -d)"
  export KUBERNETES_SERVER_CA
}

SECRETS_DIR="${ROOT}/.netci-gate/jenkins/secrets"

write_secrets() {
  # JCasC resolves ${NAME} from a file named NAME under /run/secrets when no such
  # environment variable exists (ADR-033). Secrets therefore reach the controller as
  # files that can be rewritten and re-read with a JCasC reload -- a rotation needs no
  # container recreate -- and never appear in `docker inspect`.
  # The controller runs as uid 1000 (jenkins) and this directory belongs to the lab
  # user; the files are readable by "other" inside the container's mount only. A real
  # deployment gives the directory to the service's uid (or lets a secret manager
  # mount it) and keeps 0400.
  mkdir -p "${SECRETS_DIR}"
  chmod 755 "${SECRETS_DIR}"
  ( umask 022; printf '%s' "${NETCI_PIPELINE_API_KEY}" > "${SECRETS_DIR}/NETCI_PIPELINE_API_KEY"
    printf '%s' "${NETCI_COSIGN_PRIVATE_KEY}" > "${SECRETS_DIR}/NETCI_COSIGN_PRIVATE_KEY" )
  chmod 644 "${SECRETS_DIR}"/NETCI_*
  [[ -s "${SECRETS_DIR}/KUBERNETES_SERVICE_ACCOUNT_TOKEN" ]] || write_sa_token
}

write_sa_token() {
  # The token is *bound* to a Secret object: deleting that Secret invalidates the token
  # at once, which is what makes a rotation a revocation and not merely a new token
  # beside a live old one. The anchor's name is kept so the next rotation can revoke it.
  local anchor="jenkins-controller-token-$(date -u +%Y%m%d-%H%M%S)"
  kubectl -n netci-build create secret generic "${anchor}" --from-literal=purpose=token-anchor >/dev/null
  kubectl -n netci-build label secret "${anchor}" netci.io/token-anchor=jenkins-controller >/dev/null
  local uid; uid="$(kubectl -n netci-build get secret "${anchor}" -o jsonpath='{.metadata.uid}')"
  local tmp="${SECRETS_DIR}/.KUBERNETES_SERVICE_ACCOUNT_TOKEN.new"
  ( umask 022; kubectl -n netci-build create token jenkins-controller --duration="${NETCI_SA_TOKEN_TTL:-24h}" \
      --bound-object-kind Secret --bound-object-name "${anchor}" --bound-object-uid "${uid}" > "${tmp}" )
  chmod 644 "${tmp}"
  # Atomic: JCasC never reads a half-written token.
  mv -f "${tmp}" "${SECRETS_DIR}/KUBERNETES_SERVICE_ACCOUNT_TOKEN"
  PREVIOUS_TOKEN_ANCHOR="$(cat "${SECRETS_DIR}/.token-anchor" 2>/dev/null || true)"
  printf '%s' "${anchor}" > "${SECRETS_DIR}/.token-anchor"
  log "wrote a fresh 24h service-account token to ${SECRETS_DIR}/KUBERNETES_SERVICE_ACCOUNT_TOKEN"
}

reload_casc() {
  # Ask each controller to re-read jenkins/casc (mounted) and /run/secrets (mounted).
  local letter ip jar crumb rc=0
  for letter in a b; do
    ip="${CONTROLLER_IP[$letter]}"
    curl -sf --max-time 5 -u "admin:${ADMIN_PASSWORD}" "http://${ip}:8080/api/json?tree=mode" >/dev/null 2>&1 \
      || { log "jenkins-${letter} is not up; skipped"; continue; }
    jar="${ROOT}/.netci-gate/jenkins/cookies-${letter}"
    crumb="$(curl -sf -c "${jar}" -u "admin:${ADMIN_PASSWORD}" \
      "http://${ip}:8080/crumbIssuer/api/json" | sed -n 's/.*"crumb":"\([^"]*\)".*/\1/p')"
    if curl -sf -o /dev/null -b "${jar}" -u "admin:${ADMIN_PASSWORD}" -H "Jenkins-Crumb: ${crumb}" -X POST \
         "http://${ip}:8080/configuration-as-code/reload"; then
      log "jenkins-${letter} reloaded its JCasC"
    else
      log "jenkins-${letter} refused the JCasC reload; recreate it instead"; rc=1
    fi
  done
  return $rc
}

rotate_token() {
  # The controllers' Kubernetes credential is a bound service-account token with a 24h
  # life. Rotation: mint a new one into the secrets file, reload JCasC (the kubernetes
  # plugin uses the credential on its next pod launch), then revoke the previous token
  # by deleting the Secret it was bound to.
  write_sa_token
  if command -v curl >/dev/null && [[ -n "${NETCI_CASC_WEBHOOK_SECRET:-}" ]]; then
    # Through netCI when it is up: the reload is audited and followed by a drift check.
    local body='{"event":"token-rotated","controllers":["jenkins-a","jenkins-b"]}'
    local sig; sig="sha256=$(printf '%s' "${body}" | openssl dgst -sha256 -hmac "${NETCI_CASC_WEBHOOK_SECRET}" | sed 's/^.* //')"
    if curl -sf -X POST -H "Content-Type: application/json" -H "X-NetCI-Signature: ${sig}" \
         --data "${body}" "http://127.0.0.1:${NETCI_API_PORT}/api/v1/ci/controllers/reload" > "${ROOT}/.netci-gate/jenkins/last-reload.json"; then
      log "netCI reloaded the controllers: $(python3 -c "import json;d=json.load(open('${ROOT}/.netci-gate/jenkins/last-reload.json'));print(d['controllers'], 'drift' if d['drift']['drift'] else 'no drift')")"
    else
      log "netCI reload endpoint refused; reloading the controllers directly"; reload_casc
    fi
  else
    reload_casc
  fi
  if [[ -n "${PREVIOUS_TOKEN_ANCHOR:-}" ]]; then
    kubectl -n netci-build delete secret "${PREVIOUS_TOKEN_ANCHOR}" --ignore-not-found >/dev/null
    log "revoked the previous token (anchor ${PREVIOUS_TOKEN_ANCHOR} deleted)"
  fi
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
    -v "${SECRETS_DIR}:/run/secrets:ro" \
    -e KUBERNETES_API_URL="https://${CLUSTER}-control-plane:6443" \
    -e KUBERNETES_SERVER_CA="${KUBERNETES_SERVER_CA}" \
    -e KUBERNETES_CREDENTIALS_ID=netci-kind-token \
    -e JENKINS_AGENT_URL="http://${ip}:8080" \
    -e JENKINS_SHARED_LIBRARY_REMOTE="http://${GIT_SERVER_IP}/netci-shared-library.git" \
    -e JENKINS_INBOUND_AGENT_IMAGE="${REGISTRY}/${INBOUND_AGENT_IMAGE}" \
    -e JENKINS_TOOLBOX_IMAGE="${REGISTRY}/${TOOLBOX_IMAGE}" \
    -e JAVA_OPTS="-Djenkins.install.runSetupWizard=false" \
    "${CONTROLLER_IMAGE}" >/dev/null
  log "started ${name} at http://${ip}:8080"
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
  write_secrets
  for letter in a b; do
    start_controller "${letter}"
  done
  for letter in a b; do
    wait_for_controller "${CONTROLLER_IP[$letter]}" "jenkins-${letter}"
  done
  summary
}

recreate() {
  local letter="${1:?usage: jenkins_lab.sh recreate <a|b>}"
  require_cluster
  cosign_key
  kube_credentials
  write_secrets
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
  reload_casc || true
  log "controllers re-clone the shared library on the next build"
}

case "${1:-up}" in
  up) up ;;
  publish) publish ;;
  reload) reload_casc ;;
  rotate-token) rotate_token ;;
  recreate) recreate "${2:-}" ;;
  status) status ;;
  down) down ;;
  gateway) kind_gateway ;;
  *) echo "usage: scripts/jenkins_lab.sh {up|publish|reload|rotate-token|recreate <a|b>|status|down|gateway}" >&2; exit 2 ;;
esac
