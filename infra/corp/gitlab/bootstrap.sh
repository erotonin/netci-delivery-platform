#!/usr/bin/env bash
# The company GitLab's starting content: an admin API token for automation (in
# .netci-gate/corp, never printed), group `platform` with the shared library (tagged) and one
# application repository copied from the old lab's git server. Idempotent.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
SECRETS="${ROOT}/.netci-gate/corp"
URL="http://172.17.0.1:8929"
LIB_TAG="${LIB_TAG:-netci-0.3.0}"
if [[ ! -s "${SECRETS}/gitlab_admin_token" ]]; then
  token="glpat-$(python3 -c 'import secrets;print(secrets.token_urlsafe(20))')"
  # The token reaches the container on stdin only, never on a command line or in a log.
  printf '%s' "${token}" | docker exec -i netci-corp-gitlab gitlab-rails runner '
    value = STDIN.read.strip
    user = User.find_by_username("root")
    t = user.personal_access_tokens.create!(name: "netci-bootstrap", scopes: [:api, :write_repository], expires_at: 365.days.from_now)
    t.set_token(value); t.save!' >/dev/null
  ( umask 077; printf '%s' "${token}" > "${SECRETS}/gitlab_admin_token" )
fi
api() { curl -fsS -H "PRIVATE-TOKEN: $(cat "${SECRETS}/gitlab_admin_token")" -H 'Content-Type: application/json' "$@"; }
group_id=$(api "${URL}/api/v4/groups?search=platform" | python3 -c 'import json,sys;g=[x for x in json.load(sys.stdin) if x["path"]=="platform"];print(g[0]["id"] if g else "")')
[[ -n "${group_id}" ]] || group_id=$(api -X POST "${URL}/api/v4/groups" -d '{"name":"platform","path":"platform","visibility":"internal"}' | python3 -c 'import json,sys;print(json.load(sys.stdin)["id"])')
ensure_project() {
  api "${URL}/api/v4/projects/platform%2F$1" >/dev/null 2>&1 \
    || api -X POST "${URL}/api/v4/projects" -d "{\"name\":\"$1\",\"namespace_id\":${group_id},\"visibility\":\"internal\",\"initialize_with_readme\":false}" >/dev/null
}
ensure_project netci-shared-library
ensure_project payments-api
# Push over HTTP with the token as an ASKPASS answer, never in the remote URL.
askpass="$(mktemp)"; trap 'rm -f "${askpass}"' EXIT
printf '#!/bin/sh\ncase "$1" in Username*) echo root;; *) cat "%s";; esac\n' "${SECRETS}/gitlab_admin_token" > "${askpass}"; chmod 700 "${askpass}"
work="$(mktemp -d)"; trap 'rm -rf "${work}" "${askpass}"' EXIT
mkdir -p "${work}/lib" && cp -r "${ROOT}/jenkins/shared-library/vars" "${ROOT}/jenkins/shared-library/resources" "${work}/lib/"
( cd "${work}/lib" && git init -q -b main && git add -A && git -c user.name=netci -c user.email=netci@corp.local commit -q -m "netCI shared library ${LIB_TAG}" \
  && git tag -f "${LIB_TAG}" \
  && GIT_ASKPASS="${askpass}" git push -q -f "${URL}/platform/netci-shared-library.git" main "refs/tags/${LIB_TAG}" )
git clone -q --bare "${ROOT}/.netci-gate/git/payments-api.git" "${work}/app.git"
( cd "${work}/app.git" && GIT_ASKPASS="${askpass}" git push -q -f "${URL}/platform/payments-api.git" main )
echo "gitlab: group platform, netci-shared-library@${LIB_TAG}, payments-api"
