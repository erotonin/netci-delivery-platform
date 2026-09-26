#!/usr/bin/env bash
# Copy the Trivy vulnerability DB from its source into the lab Harbor's mirror (ADR-056).
#
#   scripts/corp/mirror_trivy_db.sh        # run by the netci-trivy-db-mirror user timer
#
# Every scan reads the DB from 172.17.0.1:8930/mirror/aquasec/trivy-db:2, and the toolchain
# gate refuses a build whose DB was older than trivyDb.maxAgeHours when it scanned -- so the
# mirror must be refreshed on a schedule, not when someone remembers. Harbor's own replication
# would do this, but on this host Docker runs with iptables off and its containers have no
# egress; the host does. Succeeds only when the mirror serves the upstream digest.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
C="${ROOT}/.netci-gate/corp"
COSIGN="${ROOT}/.netci-gate/bin/cosign"
SOURCE=ghcr.io/aquasecurity/trivy-db:2
TARGET=localhost:8930/mirror/aquasec/trivy-db:2
log() { printf '[%s] %s\n' "$(date -Is)" "$*"; }

# The operators' robot (push to mirror); builds' robot cannot write here. Its login lives in a
# throwaway DOCKER_CONFIG so the credential is not left in ~/.docker.
DOCKER_CONFIG="$(mktemp -d)"; export DOCKER_CONFIG
trap 'rm -rf "${DOCKER_CONFIG}"' EXIT
docker login localhost:8930 -u "$(<"${C}/harbor_ops_robot_name")" --password-stdin \
  < "${C}/harbor_ops_robot_secret" >/dev/null 2>&1

"${COSIGN}" copy --allow-http-registry --allow-insecure-registry -f "${SOURCE}" "${TARGET}" >/dev/null

upstream=$("${COSIGN}" triangulate --type digest "${SOURCE}" 2>/dev/null | sed 's/.*@//' || true)
mirrored=$("${COSIGN}" triangulate --allow-http-registry --allow-insecure-registry --type digest "${TARGET}" 2>/dev/null | sed 's/.*@//' || true)
if [[ -z "${upstream}" || "${upstream}" != "${mirrored}" ]]; then
  log "FAIL: mirror serves '${mirrored}', upstream is '${upstream}'"; exit 1
fi
log "trivy-db mirrored: ${mirrored}"
