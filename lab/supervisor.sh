#!/usr/bin/env bash
# Install the cell supervisor on the lab cluster, with this host as its power controller.
#
#   NETCI_IMAGE=172.17.0.1:8930/netci/netci:<tag> lab/supervisor.sh
#
# What it changes on this host: one line in ~/.ssh/authorized_keys, for a key generated here
# and handed to the supervisor. The line is restricted (no shell, no forwarding, no pty), only
# accepted from the three lab guests, and bound to a forced command that can only read or switch
# the power of guests named netci-lab-<n>. Remove the line ending in "netci-fence" to revoke it.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STATE="${ROOT}/.netci-gate/lab"
export KUBECONFIG="${STATE}/kubeconfig"
: "${NETCI_IMAGE:?set NETCI_IMAGE to the image to run (a Harbor reference)}"
GUESTS="192.168.122.211,192.168.122.212,192.168.122.213"
MACHINES="netci-lab-1=netci-lab-1,netci-lab-2=netci-lab-2,netci-lab-3=netci-lab-3"
AGENT="${HOME}/.local/libexec/netci-fence"

# 1. The fencing key, generated once and kept with the lab's other secrets.
mkdir -m 700 -p "${STATE}/fence"
[[ -s "${STATE}/fence/id_ed25519" ]] || ssh-keygen -q -t ed25519 -N "" -C netci-fence -f "${STATE}/fence/id_ed25519"
cp /etc/ssh/ssh_host_ed25519_key.pub "${STATE}/fence/host_key.pub"

# 2. The power agent, copied out of the working tree: the forced command must not change when
# a branch is checked out.
install -D -m 0755 "${ROOT}/lab/fence/netci-fence" "${AGENT}"

# 3. The restricted authorized_keys line, replaced if present.
mkdir -m 700 -p "${HOME}/.ssh"
touch "${HOME}/.ssh/authorized_keys"; chmod 600 "${HOME}/.ssh/authorized_keys"
line="restrict,from=\"${GUESTS}\",command=\"${AGENT}\" $(cut -d' ' -f1,2 "${STATE}/fence/id_ed25519.pub") netci-fence"
{ /usr/bin/grep -a -v ' netci-fence$' "${HOME}/.ssh/authorized_keys" || true; echo "${line}"; } > "${HOME}/.ssh/authorized_keys.new"
mv "${HOME}/.ssh/authorized_keys.new" "${HOME}/.ssh/authorized_keys"

# 4. The cluster side. Secrets go through files, never a command line.
kubectl create namespace netci-system --dry-run=client -o yaml | kubectl apply -f - >/dev/null
kubectl -n netci-system create secret generic netci-fence \
  --from-file=id_ed25519="${STATE}/fence/id_ed25519" --from-file=host_key.pub="${STATE}/fence/host_key.pub" \
  --dry-run=client -o yaml | kubectl apply -f - >/dev/null
kubectl -n netci-system create secret generic harbor-pull --type kubernetes.io/dockerconfigjson \
  --from-file=.dockerconfigjson="${STATE}/harbor-pull.json" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
sed -e "s|NETCI_IMAGE|${NETCI_IMAGE}|" -e "s|NETCI_MACHINES_VALUE|${MACHINES}|" \
    -e "s|NETCI_FENCE_ADDR_VALUE|192.168.122.1:22|" -e "s|NETCI_FENCE_USER_VALUE|$(id -un)|" \
    -e "s|NETCI_AUTO_POWER_ON_VALUE|true|" "${ROOT}/deploy/supervisor/supervisor.yaml" | kubectl apply -f - >/dev/null
kubectl -n netci-system rollout status deployment/netci-supervisor --timeout=180s
