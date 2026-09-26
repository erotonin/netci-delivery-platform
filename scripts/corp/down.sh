#!/usr/bin/env bash
# Stop the company-shaped lab.
#
#   scripts/corp/down.sh            stop containers, keep the cluster and every volume
#   scripts/corp/down.sh --delete   also delete the kind cluster and the containers' volumes
#
# Never deletes .netci-gate/corp: GitLab, Harbor and the S3 store were initialised with
# those secrets, and the Kopia repository key there is the only way to read old backups.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
C="${ROOT}/.netci-gate/corp"
DELETE=false
for arg in "$@"; do case "$arg" in --delete) DELETE=true ;; *) echo "unknown $arg" >&2; exit 2 ;; esac; done
volumes=(); ${DELETE} && volumes=(-v)
log() { printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*"; }

log "GitLab"
docker compose -f "${ROOT}/infra/corp/gitlab/docker-compose.yml" down "${volumes[@]}"
log "SeaweedFS"
docker compose -f "${ROOT}/infra/corp/seaweedfs/docker-compose.yml" down "${volumes[@]}"
log "Harbor"
if [[ -d "${C}/harbor-installer/harbor" ]]; then
  # Installed as root (its data directory is root-owned), so stopped the same way.
  ( cd "${C}/harbor-installer/harbor" && sudo -n docker compose down "${volumes[@]}" )
fi
if ${DELETE}; then
  log "kind cluster netci-corp"
  kind delete cluster --name netci-corp
else
  log "kind cluster netci-corp kept (--delete removes it)"
fi
