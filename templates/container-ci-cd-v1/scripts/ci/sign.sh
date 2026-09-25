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
# cosign 3.x loads a signing config by default and then refuses --tlog-upload=false, and
# refuses an explicit --rekor-url alongside it; cosign 2.x has no such flag. Detect it
# rather than pinning the pipeline to one major version.
if cosign sign --help 2>&1 | grep -q -- '--use-signing-config'; then
  cosign_args+=(--use-signing-config=false)
fi

cosign "${cosign_args[@]}" "${registry_artifact_ref}"
cosign "${verify_args[@]}" --output json "${registry_artifact_ref}" \
  > "${NETCI_OUTPUT_DIR}/signature.bundle.json"
require_file "${NETCI_OUTPUT_DIR}/signature.bundle.json"

# SLSA v1 provenance, attested with the same key: which repository, which commit, which
# directory, which Jenkins build. The worker verifies it at deploy time and compares the
# commit and repository with what netCI dispatched, so an image signed with netCI's key
# but built from something else is refused -- without asking netCI's database.
if [[ "${COSIGN_ATTEST_PROVENANCE:-true}" == "true" ]]; then
  if [[ -z "${GIT_URL:-}" || -z "${COMMIT_SHA:-}" ]]; then
    echo "not dispatched by netCI (no GIT_URL/COMMIT_SHA): no provenance to attest" >&2
  else
    callback="${NETCI_TOOLING_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)}/scripts/netci_callback.py"
    python3 "${callback}" provenance --output provenance.json
    attest_args=(attest --yes --key "${COSIGN_KEY_REF}" --type slsaprovenance1 \
      --predicate "${NETCI_OUTPUT_DIR}/provenance.json")
    verify_attest_args=(verify-attestation --key "${COSIGN_PUBLIC_KEY_REF}" --type slsaprovenance1)
    if [[ "${COSIGN_ALLOW_INSECURE_REGISTRY:-true}" == "true" ]]; then
      attest_args+=(--allow-insecure-registry)
      verify_attest_args+=(--allow-insecure-registry)
    fi
    if [[ "${COSIGN_TLOG_UPLOAD:-false}" == "false" ]]; then
      attest_args+=(--tlog-upload=false)
      verify_attest_args+=(--insecure-ignore-tlog=true)
    else
      attest_args+=(--rekor-url "${COSIGN_REKOR_URL}")
      verify_attest_args+=(--rekor-url "${COSIGN_REKOR_URL}")
    fi
    if cosign attest --help 2>&1 | grep -q -- '--use-signing-config'; then
      attest_args+=(--use-signing-config=false)
    fi
    cosign "${attest_args[@]}" "${registry_artifact_ref}"
    cosign "${verify_attest_args[@]}" "${registry_artifact_ref}" > "${NETCI_OUTPUT_DIR}/provenance.verify.json"
    require_file "${NETCI_OUTPUT_DIR}/provenance.verify.json"
  fi
fi
