#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

require_command trivy

# netCI blocks findings that a rebuild can actually fix. Vulnerabilities with no
# published fix are still recorded in the report (and in the evidence netCI stores),
# but they cannot be actioned by re-running CI, so failing every build on them would
# make the gate meaningless rather than strict. Set TRIVY_IGNORE_UNFIXED=false to
# block on unfixed findings as well.
TRIVY_IGNORE_UNFIXED="${TRIVY_IGNORE_UNFIXED:-true}"
trivy_flags=(--severity HIGH,CRITICAL --scanners vuln --format json)
# A mirrored vulnerability database, for build agents with no route to the internet.
if [[ -n "${TRIVY_DB_REPOSITORY:-}" ]]; then
  trivy_flags+=(--db-repository "${TRIVY_DB_REPOSITORY}")
fi
if [[ "${TRIVY_IGNORE_UNFIXED}" == "true" ]]; then
  trivy_flags+=(--ignore-unfixed)
fi
if [[ "${REGISTRY_TLS_VERIFY}" == "false" ]]; then
  trivy_flags+=(--insecure)
fi

if [[ "${NETCI_PUBLISH}" == "false" ]]; then
  # A verify-only build (a fork's pull request, ADR-043) is scanned from the archive
  # build.sh wrote and never pushed. Trivy cannot open that archive as a tar ("manifest.json
  # not found", then "index.json: not a directory"), but it reads the OCI layout inside
  # it, so the layout is unpacked into a temporary directory -- not into the evidence
  # directory, which is archived -- and removed on exit. Not under WORKSPACE_TMP: Jenkins
  # names that `<job>@tmp`, and trivy reads `dir@...` as a digest reference and looks for
  # `<job>/index.json` instead (seen with trivy 0.71).
  require_file "${OCI_ARCHIVE}"
  layout_dir="$(mktemp -d "${TMPDIR:-/tmp}/netci-scan.XXXXXX")"
  trap 'rm -rf "${layout_dir}"' EXIT
  if [[ "${layout_dir}" == *[@:]* ]]; then
    echo "trivy cannot be given a layout path containing '@' or ':': ${layout_dir}" >&2
    exit 1
  fi
  tar -xf "${OCI_ARCHIVE}" -C "${layout_dir}"
  scan_target=(--input "${layout_dir}")
else
  # Scan the image in the registry, by digest: it is exactly the artifact that will be
  # deployed. push-image.sh is idempotent, so calling it here does not re-upload on the
  # later sign/publish stages.
  bash "$(dirname "${BASH_SOURCE[0]}")/push-image.sh"
  scan_target=("$(tr -d '\r\n' < "${NETCI_OUTPUT_DIR}/registry-artifact-ref.txt")")
fi

trivy image \
  --exit-code 1 \
  "${trivy_flags[@]}" \
  --output "${NETCI_OUTPUT_DIR}/scan-report.json" \
  "${scan_target[@]}"
require_file "${NETCI_OUTPUT_DIR}/scan-report.json"
