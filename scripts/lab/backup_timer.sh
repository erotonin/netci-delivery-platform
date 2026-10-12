#!/usr/bin/env bash
# Install the backup, DR-drill and Jenkins-token-rotation timers in the *user* systemd
# instance for the lab, with the lab profile as the environment file. Production installs
# infra/systemd/* system-wide.
set -euo pipefail
cd "$(dirname "$0")/../.."
root="$PWD"
units="$HOME/.config/systemd/user"; mkdir -p "$units"
envfile="$root/.netci-gate/backup.env"
set -a; source .netci-gate/real-local.env; set +a
: "${DATABASE_URL:?}" "${NETCI_BACKUP_ENCRYPTION_KEY:?}"
umask 077
cat > "$envfile" <<ENV
DATABASE_URL=$DATABASE_URL
NETCI_BACKUP_ENCRYPTION_KEY=$NETCI_BACKUP_ENCRYPTION_KEY
NETCI_BACKUP_DIR=$root/.netci-gate/backups
NETCI_BACKUP_KEEP=${NETCI_BACKUP_KEEP:-7}
ENV
rotatefile="$root/.netci-gate/jenkins-rotate.env"
cat > "$rotatefile" <<ENV
KUBECONFIG=${KUBECONFIG:-$HOME/.kube/config}
JENKINS_ADMIN_PASSWORD=$JENKINS_A_API_TOKEN
NETCI_CASC_WEBHOOK_SECRET=${NETCI_CASC_WEBHOOK_SECRET:-}
NETCI_API_PORT=8100
PATH=/usr/local/bin:/usr/bin:/bin
ENV
for unit in netci-backup.service netci-backup.timer netci-dr-drill.service netci-dr-drill.timer \
            netci-jenkins-token-rotate.service netci-jenkins-token-rotate.timer; do
  sed -e "s#/etc/netci/backup.env#$envfile#" -e "s#/etc/netci/jenkins-rotate.env#$rotatefile#" -e "s#/opt/netci#$root#g" "infra/systemd/$unit" > "$units/$unit"
done
systemctl --user daemon-reload
systemctl --user enable --now netci-backup.timer netci-dr-drill.timer netci-jenkins-token-rotate.timer
systemctl --user list-timers --no-pager | grep -E "netci|NEXT"
