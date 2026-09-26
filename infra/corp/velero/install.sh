#!/usr/bin/env bash
# Velero with its Kopia file-system uploader, backing up to the lab's S3 (SeaweedFS, ADR-055).
# The CLI and the credentials file live under .netci-gate/corp: neither belongs in the repository.
set -euo pipefail
VERSION="v1.18.3"
# Pulled from the company registry's mirror project: cluster nodes have no route to Docker Hub.
MIRROR="${MIRROR:-172.17.0.1:8930/mirror}"
PLUGIN="${MIRROR}/velero/velero-plugin-for-aws:v1.14.3"     # v1.14.x is the line for Velero v1.18.x
CONTEXT="${KUBE_CONTEXT:-kind-netci-corp}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
SECRETS="${ROOT}/.netci-gate/corp"
BIN="${SECRETS}/bin"
mkdir -p "${BIN}"
if [[ ! -x "${BIN}/velero" ]]; then
  curl -fsSL "https://github.com/vmware-tanzu/velero/releases/download/${VERSION}/velero-${VERSION}-linux-amd64.tar.gz" \
    | tar -xz -C "${BIN}" --strip-components=1 "velero-${VERSION}-linux-amd64/velero"
fi
( umask 077
  printf '[default]\naws_access_key_id=%s\naws_secret_access_key=%s\n' \
    "$(cat "${SECRETS}/s3-access-key")" "$(cat "${SECRETS}/s3-secret-key")" > "${SECRETS}/velero-credentials" )
"${BIN}/velero" install --kubecontext "${CONTEXT}" \
  --provider aws --plugins "${PLUGIN}" --image "${MIRROR}/velero/velero:${VERSION}" \
  --bucket netci-jenkins-backups --secret-file "${SECRETS}/velero-credentials" \
  --use-node-agent --uploader-type kopia --default-volumes-to-fs-backup \
  --backup-location-config region=us-east-1,s3ForcePathStyle=true,s3Url=http://172.17.0.1:8333 \
  --use-volume-snapshots=false --wait
# The Kopia repository key. Velero's own default is a published constant, and JENKINS_HOME
# holds credentials.xml *and* secrets/master.key that decrypts it: with the default, anyone
# who can read the bucket can read every Jenkins credential. Set before the first backup --
# the repository is initialised with whatever key is in place then.
( umask 077
  [[ -s "${SECRETS}/velero-repo-password" ]] || openssl rand -base64 32 | tr -d '\n' > "${SECRETS}/velero-repo-password" )
kubectl --context "${CONTEXT}" -n velero create secret generic velero-repo-credentials \
  --from-file=repository-password="${SECRETS}/velero-repo-password" --dry-run=client -o yaml \
  | kubectl --context "${CONTEXT}" apply -f - >/dev/null
kubectl --context "${CONTEXT}" apply -f "${ROOT}/infra/corp/velero/schedule.yaml"
"${BIN}/velero" --kubecontext "${CONTEXT}" backup-location get
