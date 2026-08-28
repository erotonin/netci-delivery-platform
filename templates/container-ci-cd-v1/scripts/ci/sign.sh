#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

require_command cosign
if [[ -z "${COSIGN_KEY_REF:-}" ]]; then
  echo "COSIGN_KEY_REF is required; refusing unsigned publish" >&2
  exit 1
fi

# Cosign signs a remote OCI digest, so the image must be in the registry first.
bash "$(dirname "${BASH_SOURCE[0]}")/push-image.sh"
registry_artifact_ref="$(tr -d '\r\n' < "${NETCI_OUTPUT_DIR}/registry-artifact-ref.txt")"

# An image signature lives in the registry beside the image, not in a local bundle
# file (that is `sign-blob`). The evidence netCI stores is therefore the *verification*
# result, which is the fact that actually matters: this key verifies this digest.
cosign_args=(sign --yes --key "${COSIGN_KEY_REF}" \
  --output-signature "${NETCI_OUTPUT_DIR}/signature.sig" \
  --output-certificate "${NETCI_OUTPUT_DIR}/signature.cert")
# Verification needs the public half. Deriving it from the private key means CI only
# ever has to be given one secret, and the key it verifies with is provably the pair
# of the key it signed with.
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
# cosign 3.x loads a signing config by default and then refuses --tlog-upload=false;
# cosign 2.x has no such flag. Detect it rather than pinning the pipeline to one major
# version, because the agent toolbox and a developer's workstation often differ.
if [[ "${COSIGN_TLOG_UPLOAD:-false}" == "false" ]] && cosign sign --help 2>&1 | grep -q -- '--use-signing-config'; then
  cosign_args+=(--use-signing-config=false)
fi

cosign "${cosign_args[@]}" "${registry_artifact_ref}"
cosign "${verify_args[@]}" --output json "${registry_artifact_ref}" \
  > "${NETCI_OUTPUT_DIR}/signature.bundle.json"
require_file "${NETCI_OUTPUT_DIR}/signature.bundle.json"
