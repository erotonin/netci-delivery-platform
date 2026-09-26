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
# The token reaches curl as config on stdin: as an argument it would show in `ps`.
api() { printf 'header = "PRIVATE-TOKEN: %s"\n' "$(<"${SECRETS}/gitlab_admin_token")" | curl -K - -fsS -H 'Content-Type: application/json' "$@"; }
group_id=$(api "${URL}/api/v4/groups?search=platform" | python3 -c 'import json,sys;g=[x for x in json.load(sys.stdin) if x["path"]=="platform"];print(g[0]["id"] if g else "")')
[[ -n "${group_id}" ]] || group_id=$(api -X POST "${URL}/api/v4/groups" -d '{"name":"platform","path":"platform","visibility":"internal"}' | python3 -c 'import json,sys;print(json.load(sys.stdin)["id"])')
ensure_project() {
  api "${URL}/api/v4/projects/platform%2F$1" >/dev/null 2>&1 \
    || api -X POST "${URL}/api/v4/projects" -d "{\"name\":\"$1\",\"namespace_id\":${group_id},\"visibility\":\"internal\",\"initialize_with_readme\":false}" >/dev/null
}
ensure_project netci-shared-library
ensure_project payments-api
# Webhooks to the local network stay refused (SSRF), except to netCI's ingress name.
api -X PUT "${URL}/api/v4/application/settings" \
  -d '{"allow_local_requests_from_web_hooks_and_services":false,"outbound_local_requests_whitelist":["netci.corp.local"]}' >/dev/null
# Least-privilege group tokens, 90 days, instead of the admin token: Jenkins clones with a
# read_repository/Reporter token, netCI's pipeline designer pushes branches and opens merge
# requests with an api/Developer one (it cannot protect branches or merge past approvals).
group_token() {  # $1 file  $2 name  $3 scopes-json  $4 access level
  [[ -s "${SECRETS}/$1" ]] && return 0
  ( umask 077; api -X POST "${URL}/api/v4/groups/${group_id}/access_tokens" \
      -d "{\"name\":\"$2\",\"scopes\":$3,\"access_level\":$4,\"expires_at\":\"$(date -d '+90 days' +%F)\"}" \
      | python3 -c 'import json,sys;print(json.load(sys.stdin)["token"],end="")' > "${SECRETS}/$1" )
}
group_token gitlab_jenkins_token jenkins-clone '["read_repository"]' 20
group_token gitlab_netci_token netci-designer '["api"]' 30
# Push over HTTP with the token as an ASKPASS answer, never in the remote URL.
askpass="$(mktemp)"; trap 'rm -f "${askpass}"' EXIT
printf '#!/bin/sh\ncase "$1" in Username*) echo root;; *) cat "%s";; esac\n' "${SECRETS}/gitlab_admin_token" > "${askpass}"; chmod 700 "${askpass}"
work="$(mktemp -d)"; trap 'rm -rf "${work}" "${askpass}"' EXIT
# A new library version is a commit on top of main and a new tag -- never a force push:
# main is protected, and a released tag that moved would change what every job pinned to it
# runs without anyone changing the job.
lib_url="${URL}/platform/netci-shared-library.git"
if GIT_ASKPASS="${askpass}" git ls-remote --exit-code --tags "${lib_url}" "refs/tags/${LIB_TAG}" >/dev/null 2>&1; then
  echo "gitlab: ${LIB_TAG} already exists; a released tag is not moved (bump LIB_TAG)"
else
  if GIT_ASKPASS="${askpass}" git ls-remote --exit-code --heads "${lib_url}" main >/dev/null 2>&1; then
    GIT_ASKPASS="${askpass}" git clone -q "${lib_url}" "${work}/lib"
  else
    git init -q -b main "${work}/lib"
  fi
  ( cd "${work}/lib" && find . -mindepth 1 -maxdepth 1 ! -name .git -exec rm -rf {} + \
    && cp -r "${ROOT}/jenkins/shared-library/vars" "${ROOT}/jenkins/shared-library/resources" . \
    && git add -A \
    && { git diff --cached --quiet || git -c user.name=netci -c user.email=netci@corp.local commit -q -m "netCI shared library ${LIB_TAG}"; } \
    && git -c user.name=netci -c user.email=netci@corp.local tag -a "${LIB_TAG}" -m "netCI shared library ${LIB_TAG}" \
    && GIT_ASKPASS="${askpass}" git push -q "${lib_url}" main "refs/tags/${LIB_TAG}" )
fi
git clone -q --bare "${ROOT}/.netci-gate/git/payments-api.git" "${work}/app.git"
# The application's history is its own: pushed once, then changed only through merge requests.
GIT_ASKPASS="${askpass}" git ls-remote --exit-code --heads "${URL}/platform/payments-api.git" main >/dev/null 2>&1 \
  || ( cd "${work}/app.git" && GIT_ASKPASS="${askpass}" git push -q "${URL}/platform/payments-api.git" main )
echo "gitlab: group platform, netci-shared-library@${LIB_TAG}, payments-api"
