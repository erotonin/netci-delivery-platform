#!/usr/bin/env bash
# One scheduled backup: create, verify, prune. Non-zero exit if any step fails, so the
# timer's unit state (and any alert on failed units) tells the truth.
set -euo pipefail
cd "$(dirname "$0")/.."
: "${DATABASE_URL:?}" "${NETCI_BACKUP_ENCRYPTION_KEY:?}"
dir="${NETCI_BACKUP_DIR:-/var/backups/netci}"
keep="${NETCI_BACKUP_KEEP:-14}"
mkdir -p "$dir"
before="$(ls -1d "$dir"/netci-* 2>/dev/null | sort | tail -1 || true)"
.venv/bin/python scripts/netci_backup.py create --encrypt --output "$dir"
latest="$(ls -1d "$dir"/netci-* | sort | tail -1)"
[ -n "$latest" ] && [ "$latest" != "$before" ] || { echo "no new backup folder appeared in $dir" >&2; exit 1; }
.venv/bin/python scripts/netci_backup.py verify --input "$latest"
# Prune: keep the newest $keep dated folders; only folders this script names.
ls -1d "$dir"/netci-* | sort | head -n -"$keep" | while read -r old; do rm -rf -- "$old"; done
echo "backup $latest verified; $(ls -1d "$dir"/netci-* | wc -l) kept"
