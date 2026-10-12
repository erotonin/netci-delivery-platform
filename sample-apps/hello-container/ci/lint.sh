#!/usr/bin/env bash
# A custom catalog stage (ADR-030): registered in netCI by a platform administrator and
# selected per module in the portal; it runs here, in the builder, after `unit-test`.
# It lives in the repository so that the code a build executes is reviewed in git.
set -euo pipefail
python3 -m py_compile sample-apps/hello-container/app.py
echo "NETCI_CUSTOM_STAGE=lint ok: app.py compiles"
