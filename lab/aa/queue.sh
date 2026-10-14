#!/usr/bin/env bash
# netci-queue in front of the active-active replicas (ADR-081).
#
#   NETCI_IMAGE=172.17.0.1:8930/netci/netci@sha256:<digest> lab/aa/queue.sh
#
# Needs lab/aa/store.sh install (the HA store) and phases 2-5. Secrets are generated into
# .netci-gate/aa/queue (mode 600) and reach the cluster through files, never the command line:
#   - netci-queue's database password (the role is declared in store.yaml);
#   - the API token of the Jenkins user netci-queue, made by that user (API tokens need no CSRF
#     crumb; users are replicated, so it is valid on both replicas);
#   - a client token for the lab's probes and the IDP, of which only the SHA-256 is in the config.
set -euo pipefail
cd "$(dirname "$0")/../.."
export KUBECONFIG="${KUBECONFIG:-.netci-gate/lab/kubeconfig}"
: "${NETCI_IMAGE:?NETCI_IMAGE=<netci image by digest>}"
[[ "$NETCI_IMAGE" == *@sha256:* ]] || { echo "NETCI_IMAGE must be by digest" >&2; exit 1; }
NS=aa-p2
Q=.netci-gate/aa/queue
mkdir -m 700 -p "$Q"
( umask 077
  [[ -s "$Q/db-password" ]] || openssl rand -hex 24 > "$Q/db-password"
  [[ -s "$Q/client-token" ]] || openssl rand -hex 32 > "$Q/client-token"
  [[ -s "$Q/jenkins-password" ]] || openssl rand -hex 24 > "$Q/jenkins-password"
  printf 'postgresql://queue:%s@store-rw.%s.svc:5432/netci_queue?sslmode=require&target_session_attrs=read-write' \
    "$(cat "$Q/db-password")" "$NS" > "$Q/db-url"
)

# The role and its database, on the HA store.
kubectl -n "$NS" create secret generic queue-db-role --type kubernetes.io/basic-auth \
  --from-literal=username=queue --from-file=password="$Q/db-password" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
kubectl apply -f lab/aa/store.yaml >/dev/null
kubectl -n "$NS" wait database/netci-queue --for=jsonpath='{.status.applied}'=true --timeout=300s >/dev/null

# The Jenkins user netci-queue, made by the admin, and its own API token.
if [[ ! -s "$Q/jenkins-token" ]]; then
  python3 - "$Q" <<'PY'
import base64, http.cookiejar, json, sys, urllib.parse, urllib.request
sys.path.insert(0, "lab/aa")
import phase3_probe as p3
q = sys.argv[1]
password = open(f"{q}/jenkins-password").read().strip()
admin = p3.R("r0")
code, _, _ = admin.req("/securityRealm/createAccountByAdmin", urllib.parse.urlencode({
    "username": "netci-queue", "password1": password, "password2": password,
    "fullname": "netci-queue", "email": "netci-queue@lab.invalid"}).encode(), "application/x-www-form-urlencoded")
if code not in (200, 302):
    raise SystemExit(f"creating the user netci-queue: HTTP {code}")
auth = "Basic " + base64.b64encode(f"netci-queue:{password}".encode()).decode()
op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(admin.jar))
def call(path, data=None, headers={}):
    req = urllib.request.Request(p3.VIP + path, data=data, headers={"Authorization": auth, **headers})
    return json.loads(op.open(req, timeout=20).read())
crumb = call("/crumbIssuer/api/json")
r = call("/user/netci-queue/descriptorByName/jenkins.security.ApiTokenProperty/generateNewToken",
         data=b"newTokenName=netci-queue", headers={crumb["crumbRequestField"]: crumb["crumb"]})
import os
fd = os.open(f"{q}/jenkins-token", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
os.write(fd, r["data"]["tokenValue"].encode()); os.close(fd)
PY
fi
printf 'netci-queue' > "$Q/jenkins-user"
hash="$(tr -d '\n' < "$Q/client-token" | sha256sum | cut -d' ' -f1)"
cat > "$Q/config.json" <<JSON
{"cells": {"aa": {"url": "http://haproxy-local.${NS}.svc.cluster.local", "credentials": "/etc/netci/cells/aa", "budget": 50}},
 "routes": [{"prefix": "", "cell": "aa"}],
 "clients": {"idp": {"tokenSha256": "${hash}", "jobs": [""]}},
 "secretParameters": ["NETCI_CALLBACK_TOKEN"]}
JSON
kubectl -n "$NS" create secret generic queue-db --from-file=url="$Q/db-url" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
kubectl -n "$NS" create secret generic queue-jenkins --from-file=user="$Q/jenkins-user" --from-file=token="$Q/jenkins-token" \
  --dry-run=client -o yaml | kubectl apply -f - >/dev/null
kubectl -n "$NS" create configmap queue --from-file=config.json="$Q/config.json" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
sed "s|NETCI_IMAGE|${NETCI_IMAGE}|" lab/aa/queue.yaml | kubectl apply -f - >/dev/null
kubectl -n "$NS" rollout restart deployment/queue >/dev/null
kubectl -n "$NS" rollout status deployment/queue --timeout=300s
