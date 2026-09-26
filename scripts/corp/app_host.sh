#!/usr/bin/env bash
# The corp lab's deploy target: a systemd container standing in for an application VM, reached
# over SSH by netCI's worker (Ansible) with its own key and a pinned host key.
#
#   scripts/corp/app_host.sh up      # create/start, pin the host key, prove ssh + sudo + docker
#   scripts/corp/app_host.sh down
#
# Its own host and its own key, not the live lab's netci-prod-host: deployment leases live in
# each install's database, so two installs deploying to one host would not see each other.
# Same image as scripts/lab/prod_host.sh (infra/lab-prod-host); no agent is started here.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
NAME=netci-corp-app-01
IP=172.17.0.61
IMAGE=netci/lab-prod-host:0.2.0
SSH_DIR="${ROOT}/.netci-gate/corp/ssh"
REGISTRY=172.17.0.1:8930
ssh_as() { ssh -i "${SSH_DIR}/id_ed25519" -o UserKnownHostsFile="${SSH_DIR}/known_hosts" -o StrictHostKeyChecking=yes "netci@${IP}" "$@"; }

up() {
  mkdir -p "${SSH_DIR}" && chmod 700 "${SSH_DIR}"
  [[ -f "${SSH_DIR}/id_ed25519" ]] || ssh-keygen -q -t ed25519 -N "" -C netci-corp-worker -f "${SSH_DIR}/id_ed25519"
  if ! docker inspect "${NAME}" >/dev/null 2>&1; then
    [[ -n "$(docker images -q "${IMAGE}")" ]] || docker build --network=host -q -t "${IMAGE}" "${ROOT}/infra/lab-prod-host" >/dev/null
    # Privileged because systemd needs cgroups: a lab stand-in for a VM. /var/lib/docker on
    # a volume so the inner Docker can use overlay2 (see scripts/lab/prod_host.sh).
    docker run -d --name "${NAME}" --hostname "${NAME}" --network kind --ip "${IP}" --privileged --cgroupns=host \
      --restart unless-stopped --tmpfs /run --tmpfs /run/lock -v /sys/fs/cgroup:/sys/fs/cgroup:rw \
      -v "${NAME}-docker:/var/lib/docker" "${IMAGE}" >/dev/null
    sleep 3
  fi
  docker start "${NAME}" >/dev/null
  docker cp "${SSH_DIR}/id_ed25519.pub" "${NAME}:/home/netci/.ssh/authorized_keys"
  docker exec "${NAME}" bash -c 'chown -R netci:netci /home/netci/.ssh && chmod 600 /home/netci/.ssh/authorized_keys'
  # The only registry it trusts over plain HTTP is the lab Harbor, where netCI publishes.
  docker exec "${NAME}" bash -c "printf '%s' '{\"insecure-registries\": [\"${REGISTRY}\"], \"features\": {\"containerd-snapshotter\": false}}' > /etc/docker/daemon.json && systemctl restart docker"
  # Pin the host key: netCI connects with StrictHostKeyChecking=yes.
  for _ in $(seq 1 20); do
    ssh-keyscan -t ed25519 "${IP}" 2>/dev/null > "${SSH_DIR}/known_hosts.tmp" && [[ -s "${SSH_DIR}/known_hosts.tmp" ]] && break; sleep 1
  done
  mv "${SSH_DIR}/known_hosts.tmp" "${SSH_DIR}/known_hosts"
  ssh_as 'echo ssh-ok; sudo -n true && echo become-ok; sudo docker version --format "docker {{.Server.Version}}"'
}

down() { docker rm -f "${NAME}" >/dev/null 2>&1 || true; }

case "${1:-up}" in up) up ;; down) down ;; *) echo "usage: $0 up|down" >&2; exit 2 ;; esac
