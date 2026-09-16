#!/usr/bin/env bash
# An id_token for a lab user via Keycloak's password grant (lab only; a real IdP
# issues tokens through the browser flow). Usage: kc-token.sh <user> [password]
set -euo pipefail
cd "$(dirname "$0")/../.."
set -a; source .netci-gate/real-local.env; set +a
: "${NETCI_ACCEPTANCE_OIDC_TOKEN_URL:?set in .netci-gate/real-local.env}"
: "${NETCI_ACCEPTANCE_OIDC_CLIENT_ID:?}" "${NETCI_ACCEPTANCE_OIDC_CLIENT_SECRET:?}"
curl -sf -X POST "$NETCI_ACCEPTANCE_OIDC_TOKEN_URL" \
  -d "client_id=$NETCI_ACCEPTANCE_OIDC_CLIENT_ID&client_secret=$NETCI_ACCEPTANCE_OIDC_CLIENT_SECRET&grant_type=password&username=$1&password=${2:-${NETCI_LAB_USER_PASSWORD:?}}&scope=openid" \
  | .venv/bin/python -c "import sys,json;print(json.load(sys.stdin)['id_token'])"
