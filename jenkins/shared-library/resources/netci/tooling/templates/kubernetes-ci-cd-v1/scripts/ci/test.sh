#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/common-wrapper.sh"
exec bash "${container_ci_dir}/test.sh" "$@"
