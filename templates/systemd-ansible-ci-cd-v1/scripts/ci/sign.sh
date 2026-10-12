#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

require_command cosign
require_command sha256sum
require_file "${ARTIFACT_PATH}"
# The pipeline skips Sign for a verify-only build; this is the step that uploads, so it
# refuses one too rather than rely on that alone (ADR-043/054).
if [[ "${NETCI_PUBLISH}" == "false" ]]; then
  echo "verify-only build (NETCI_PUBLISH=false): refusing to upload its artifact to the registry" >&2
  exit 1
fi
if [[ -z "${COSIGN_KEY_REF:-}" ]]; then
  echo "COSIGN_KEY_REF is required; refusing unsigned publish" >&2
  exit 1
fi

# The registry is netCI's only artifact store. A Linux binary is pushed as a one-layer
# OCI artifact and signed exactly like an image, so it has the same identity (the
# manifest digest), the same signature gate and the same verifier on the worker. No
# pre-signed upload URLs, no shared filesystem between the build agent and the worker.
blob_sha256="$(sha256sum "${ARTIFACT_PATH}" | awk '{print $1}')"
built_digest="$(tr -d '\r\n' < "${NETCI_OUTPUT_DIR}/artifact-digest.txt" 2>/dev/null || true)"
if [[ -n "${built_digest}" && "${built_digest}" != "sha256:${blob_sha256}" ]]; then
  echo "artifact changed after it was built: ${built_digest} != sha256:${blob_sha256}" >&2
  exit 1
fi

push_repository="${PUSH_ARTIFACT_REPOSITORY:-${REGISTRY_PUSH_HOST}/${NETCI_ARTIFACT_NAME}}"
deploy_repository="${DEPLOY_ARTIFACT_REPOSITORY:-${REGISTRY_PULL_HOST}/${NETCI_ARTIFACT_NAME}}"
upload_args=(upload blob -f "${ARTIFACT_PATH}" --ct application/octet-stream)
if [[ "${COSIGN_ALLOW_INSECURE_REGISTRY:-true}" == "true" ]]; then
  upload_args+=(--allow-insecure-registry)
fi
upload_log="$(mktemp)"
cosign "${upload_args[@]}" "${push_repository}:${VERSION}" 2>&1 | tee "${upload_log}"
# cosign prints the pushed reference as `<repo>@sha256:<manifest digest>` on its own line.
artifact_digest="$(grep -oE 'sha256:[0-9a-f]{64}' "${upload_log}" | tail -n 1 || true)"
rm -f "${upload_log}"
if [[ ! "${artifact_digest}" =~ ^sha256:[0-9a-f]{64}$ ]]; then
  echo "cosign upload did not report a manifest digest" >&2
  exit 1
fi
registry_artifact_ref="${push_repository}@${artifact_digest}"

cosign_args=(sign --yes --key "${COSIGN_KEY_REF}")
COSIGN_PUBLIC_KEY_REF="${COSIGN_PUBLIC_KEY_REF:-${NETCI_OUTPUT_DIR}/cosign.pub}"
if [[ ! -s "${COSIGN_PUBLIC_KEY_REF}" ]]; then
  cosign public-key --key "${COSIGN_KEY_REF}" > "${COSIGN_PUBLIC_KEY_REF}"
fi
verify_args=(verify --key "${COSIGN_PUBLIC_KEY_REF}")
if [[ "${COSIGN_ALLOW_INSECURE_REGISTRY:-true}" == "true" ]]; then
  cosign_args+=(--allow-insecure-registry)
  verify_args+=(--allow-insecure-registry)
fi
if [[ "${COSIGN_TLOG_UPLOAD:-false}" == "false" ]]; then
  cosign_args+=(--tlog-upload=false)
  verify_args+=(--insecure-ignore-tlog=true)
fi
if [[ "${COSIGN_TLOG_UPLOAD:-false}" == "true" ]]; then
  # Upload only to the transparency log netCI named. Left to cosign, it would be the
  # public Rekor: every internal image digest and signing identity, published.
  [[ -n "${COSIGN_REKOR_URL:-}" ]] || {
    echo "COSIGN_TLOG_UPLOAD=true needs COSIGN_REKOR_URL (netCI's supplyChain.rekorUrl)" >&2
    exit 1
  }
  cosign_args+=(--rekor-url "${COSIGN_REKOR_URL}")
  verify_args+=(--rekor-url "${COSIGN_REKOR_URL}")
fi
# cosign 3.x loads a signing config by default and then refuses --tlog-upload=false and
# an explicit --rekor-url alike.
if cosign sign --help 2>&1 | grep -q -- '--use-signing-config'; then
  cosign_args+=(--use-signing-config=false)
fi

cosign "${cosign_args[@]}" "${registry_artifact_ref}"
cosign "${verify_args[@]}" --output json "${registry_artifact_ref}" \
  > "${NETCI_OUTPUT_DIR}/signature.bundle.json"
require_file "${NETCI_OUTPUT_DIR}/signature.bundle.json"

# From here on the artifact's identity is the manifest digest, which is what the
# signature covers and what the worker verifies. The file's own sha256 is recorded
# beside it; the worker re-derives it from the manifest, so it is informational.
printf '%s\n' "${artifact_digest}" > "${NETCI_OUTPUT_DIR}/artifact-digest.txt"
printf '%s\n' "${blob_sha256}" > "${NETCI_OUTPUT_DIR}/artifact-blob-sha256.txt"
printf '%s\n' "${registry_artifact_ref}" > "${NETCI_OUTPUT_DIR}/registry-artifact-ref.txt"
printf '%s@%s\n' "${deploy_repository}" "${artifact_digest}" > "${NETCI_OUTPUT_DIR}/artifact-ref.txt"
