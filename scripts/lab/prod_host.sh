#!/usr/bin/env bash
# A separate "production host" for the lab: a systemd container on the kind network,
# reached over SSH with a key, with netCI's Ansible user allowed to become root. It is
# what lets the systemd runtime be exercised the way a real host is (system-scope unit,
# privilege escalation, known_hosts), instead of `ansible_connection=local` on the
# machine that runs the worker.
#
#   scripts/lab/prod_host.sh up     # create/start, install sshd+python3, print inventory line
#   scripts/lab/prod_host.sh down
set -euo pipefail
cd "$(dirname "$0")/../.."
NAME="${NETCI_PROD_HOST_NAME:-netci-prod-host}"
IP="${NETCI_PROD_HOST_IP:-172.17.0.60}"
IMAGE="${NETCI_PROD_HOST_IMAGE:-netci/lab-prod-host:0.1.0}"
SECRETS="${NETCI_ANSIBLE_SECRET_DIR:-$PWD/.netci-gate/ansible}"

up() {
  mkdir -p "$SECRETS/ssh" && chmod 700 "$SECRETS" "$SECRETS/ssh"
  [[ -f "$SECRETS/ssh/id_ed25519" ]] || ssh-keygen -q -t ed25519 -N "" -C netci-worker -f "$SECRETS/ssh/id_ed25519"
  docker rm -f "$NAME" >/dev/null 2>&1 || true
  # Privileged because systemd needs cgroups; this is a lab stand-in for a VM.
  [[ "$(docker images -q "$IMAGE")" ]] || docker build --network=host -q -t "$IMAGE" infra/lab-prod-host >/dev/null
  docker run -d --name "$NAME" --network kind --ip "$IP" --privileged --cgroupns=host \
    --tmpfs /run --tmpfs /run/lock -v /sys/fs/cgroup:/sys/fs/cgroup:rw "$IMAGE" >/dev/null
  # The image (infra/lab-prod-host) carries sshd, python3, sudo and the netci user;
  # the lab network is offline, so nothing can be installed after start.
  [[ "$(docker images -q "$IMAGE")" ]] || docker build --network=host -q -t "$IMAGE" infra/lab-prod-host >/dev/null
  sleep 3
  docker cp "$SECRETS/ssh/id_ed25519.pub" "$NAME:/home/netci/.ssh/authorized_keys"
  docker exec "$NAME" bash -c 'chown -R netci:netci /home/netci/.ssh && chmod 600 /home/netci/.ssh/authorized_keys'
  # Pin the host key: netCI passes this file with StrictHostKeyChecking=yes.
  for _ in $(seq 1 20); do ssh-keyscan -t ed25519 "$IP" 2>/dev/null > "$SECRETS/ssh/known_hosts.tmp" && [[ -s "$SECRETS/ssh/known_hosts.tmp" ]] && break; sleep 1; done
  mv "$SECRETS/ssh/known_hosts.tmp" "$SECRETS/ssh/known_hosts"
  ssh -i "$SECRETS/ssh/id_ed25519" -o UserKnownHostsFile="$SECRETS/ssh/known_hosts" -o StrictHostKeyChecking=yes netci@"$IP" 'echo ssh-ok; sudo -n true && echo become-ok; systemctl is-system-running || true'
  echo "inventory: netci-prod-01 ansible_host=$IP ansible_user=netci"
}

down() { docker rm -f "$NAME" >/dev/null 2>&1 || true; }

case "${1:-up}" in up) up ;; down) down ;; *) echo "usage: $0 up|down" >&2; exit 2 ;; esac
