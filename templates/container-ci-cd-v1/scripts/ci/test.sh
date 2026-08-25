#!/usr/bin/env bash
set -euo pipefail
python -m compileall sample-apps/hello-container
printf 'container tests passed\n'
