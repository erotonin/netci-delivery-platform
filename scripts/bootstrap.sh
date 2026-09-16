#!/usr/bin/env bash
# Bring a machine from "docker, kind and python installed" to a running netCI lab in
# production mode, or say exactly which step is missing. Every step is an *ensure*: it
# checks first and creates only what is absent, so the script is safe to re-run and its
# `--check` mode is a truthful inventory of what exists.
#
#   scripts/bootstrap.sh --check          report each component: present / missing
#   scripts/bootstrap.sh --up             create what is missing, then verify /readyz
#
# Topology (one host, Docker daemon without bridge NAT, so containers use the host
# network or fixed addresses on the kind network -- see docs/LIVE-READINESS.md §7):
#   PostgreSQL :55432   Keycloak :8180   NetBox :8080   Temporal :7233   registry :55000
#   kind netci-local (+ ingress-nginx)   Jenkins A/B 172.17.0.50/51   git 172.17.0.52
#   prod host 172.17.0.60   API :8100/:8101   Prometheus :9090   Alertmanager :9093
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
MODE="${1:---check}"
GATE="$ROOT/.netci-gate"
PROFILE="$GATE/real-local.env"
PG_IMAGE="${POSTGRES_IMAGE:-postgres:16.15-alpine3.24}"
PG_PASSWORD="${NETCI_LAB_PG_PASSWORD:-netci-local-only}"
missing=0

say()  { printf '%-34s %s\n' "$1" "$2"; }
have() { command -v "$1" >/dev/null 2>&1; }
running() { docker ps --format '{{.Names}}' | grep -Fxq "$1"; }
present() { say "$1" "present${2:+ ($2)}"; }
absent()  { say "$1" "MISSING${2:+ -- $2}"; missing=$((missing+1)); }
creating(){ say "$1" "creating…"; }
up() { [ "$MODE" = "--up" ]; }
rand() { openssl rand -hex "${1:-24}"; }

# ------------------------------------------------------------------ prerequisites
step_prereqs() {
  local ok=1
  for tool in docker kind kubectl helm python3 git openssl curl ansible-playbook; do
    if have "$tool"; then :; else say "tool: $tool" "MISSING"; ok=0; missing=$((missing+1)); fi
  done
  [ $ok = 1 ] && present "tools" "docker kind kubectl helm python3 git openssl curl ansible-playbook"
  if [ -x .venv/bin/python ]; then present "python venv"; else
    if up; then creating "python venv"; python3 -m venv .venv && .venv/bin/pip install -q -r backend/requirements.txt; else absent "python venv" ".venv"; fi; fi
  mkdir -p "$GATE/keys" "$GATE/kube" "$GATE/ansible/ssh" "$GATE/keycloak" "$GATE/jenkins/secrets"
}

# ------------------------------------------------------------------ containers
ensure_container() {  # name, description, then the docker run arguments (without -d --name)
  local name="$1" desc="$2"; shift 2
  if running "$name"; then present "$desc"; return 0; fi
  if docker ps -a --format '{{.Names}}' | grep -Fxq "$name"; then
    if up; then creating "$desc (stopped, starting)"; docker start "$name" >/dev/null; return 0; fi
    absent "$desc" "container exists but is stopped"; return 0
  fi
  if up; then creating "$desc"; docker run -d --name "$name" "$@" >/dev/null; else absent "$desc"; fi
}

step_postgres() {
  ensure_container netci-p0-pg "PostgreSQL :55432" --network host --restart unless-stopped \
    -e POSTGRES_DB=netci -e POSTGRES_USER=netci -e POSTGRES_PASSWORD="$PG_PASSWORD" -e PGPORT=55432 "$PG_IMAGE"
  if up; then
    for _ in $(seq 1 30); do docker exec netci-p0-pg pg_isready -q -p 55432 -U netci && break; sleep 1; done
    for db in netci_live netci; do
      docker exec netci-p0-pg psql -U netci -p 55432 -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname='$db'" | grep -q 1 \
        || docker exec netci-p0-pg psql -U netci -p 55432 -d postgres -qc "CREATE DATABASE $db"
    done
  fi
}

step_registry() {
  ensure_container netci-lab-registry "OCI registry :55000" --network host --restart unless-stopped \
    -e REGISTRY_HTTP_ADDR=0.0.0.0:55000 -v netci-lab-registry-data:/var/lib/registry registry:3.1.1
}

step_temporal() {
  ensure_container netci-temporal "Temporal :7233" --network host --restart unless-stopped \
    temporalio/temporal:1.8.2 server start-dev --ip 0.0.0.0 --port 7233 --ui-port 8233
}

step_keycloak() {
  local realm="$GATE/keycloak/netci-realm.json"
  if [ ! -f "$realm" ]; then
    if up; then
      creating "Keycloak realm file"
      sed -e "s/__OIDC_CLIENT_SECRET__/$(rand)/" -e "s/__LAB_USER_PASSWORD__/$(rand 12)/" infra/keycloak/netci-realm.template.json > "$realm"
      chmod 600 "$realm"
    else absent "Keycloak realm file" "$realm (rendered from infra/keycloak/netci-realm.template.json)"; fi
  else present "Keycloak realm file"; fi
  ensure_container netci-keycloak "Keycloak :8180" --network host --restart unless-stopped \
    -e KC_BOOTSTRAP_ADMIN_USERNAME=admin -e KC_BOOTSTRAP_ADMIN_PASSWORD="${KEYCLOAK_ADMIN_PASSWORD:-$(rand 12)}" \
    -e KC_HTTP_PORT=8180 -e KC_HOSTNAME=http://127.0.0.1:8180 -e KC_HTTP_ENABLED=true \
    -v "$GATE/keycloak:/opt/keycloak/data/import" quay.io/keycloak/keycloak:26.0 start-dev --import-realm
}

step_netbox() {
  local token_file="$GATE/keys/netbox-token"
  [ -f "$token_file" ] || { up && { rand 20 > "$token_file"; chmod 600 "$token_file"; }; }
  local token; token="$(cat "$token_file" 2>/dev/null || echo unset)"
  local nb_pw; nb_pw="${NETBOX_DB_PASSWORD:-netbox-local-only}"
  ensure_container netci-netbox-pg "NetBox PostgreSQL :55433" --network host --restart unless-stopped \
    -e POSTGRES_DB=netbox -e POSTGRES_USER=netbox -e POSTGRES_PASSWORD="$nb_pw" -e PGPORT=55433 -v netci-netbox-pg-data:/var/lib/postgresql/data "$PG_IMAGE"
  ensure_container netci-netbox-redis "NetBox redis :6380" --network host --restart unless-stopped redis:7-alpine redis-server --port 6380
  ensure_container netci-netbox-redis-cache "NetBox redis cache :6381" --network host --restart unless-stopped redis:7-alpine redis-server --port 6381
  ensure_container netci-netbox "NetBox :8080" --network host --restart unless-stopped \
    -e DB_HOST=127.0.0.1 -e DB_PORT=55433 -e DB_NAME=netbox -e DB_USER=netbox -e DB_PASSWORD="$nb_pw" \
    -e REDIS_HOST=127.0.0.1 -e REDIS_PORT=6380 -e REDIS_CACHE_HOST=127.0.0.1 -e REDIS_CACHE_PORT=6381 \
    -e SECRET_KEY="$(rand 32)" -e SUPERUSER_NAME=admin -e SUPERUSER_EMAIL=admin@netci.local \
    -e SUPERUSER_PASSWORD="${NETBOX_ADMIN_PASSWORD:-$(rand 12)}" -e SUPERUSER_API_TOKEN="$token" -e CORS_ORIGIN_ALLOW_ALL=True \
    netboxcommunity/netbox:v4.1-3.0.2
}

# ------------------------------------------------------------------ cluster
step_kind() {
  if kind get clusters 2>/dev/null | grep -Fxq netci-local; then present "kind cluster netci-local"; else
    if up; then creating "kind cluster netci-local"; kind create cluster --config infra/kind/kind-config.yaml --wait 120s >/dev/null; else absent "kind cluster netci-local"; fi; fi
  kind get clusters 2>/dev/null | grep -Fxq netci-local || return 0
  if kubectl --context kind-netci-local get ns netci-build prod staging dev >/dev/null 2>&1 \
     && kubectl --context kind-netci-local -n netci-build get sa jenkins-controller >/dev/null 2>&1; then present "namespaces + RBAC + registry mirror"; else
    if up; then creating "namespaces + RBAC + registry mirror"; REGISTRY_MODE=gateway REGISTRY_PORT=55000 REGISTRY_PULL_HOST=172.17.0.1:55000 bash infra/kind/local-registry.sh >/dev/null; else absent "namespaces + RBAC + registry mirror" "infra/kind/local-registry.sh (gateway mode)"; fi; fi
  for env in dev staging prod; do
    local kc="$GATE/kube/netci-${env}-kubeconfig"
    if [ -f "$kc" ]; then :; elif up; then kind get kubeconfig --name netci-local > "$kc"; chmod 600 "$kc"; else absent "kubeconfig $env" "$kc"; fi
  done
  [ -f "$GATE/kube/netci-prod-kubeconfig" ] && present "kubeconfigs dev/staging/prod"
  if kubectl --context kind-netci-local -n ingress-nginx get deploy ingress-nginx-controller >/dev/null 2>&1; then present "ingress-nginx"; else
    if up; then creating "ingress-nginx"; bash scripts/lab/ingress_nginx.sh >/dev/null; else absent "ingress-nginx" "scripts/lab/ingress_nginx.sh"; fi; fi
}

# ------------------------------------------------------------------ Jenkins, git, prod host
step_jenkins() {
  if running jenkins-a && running jenkins-b && running netci-git-server; then present "Jenkins A/B + git server"; else
    if up; then creating "Jenkins A/B + git server (several minutes)"; bash scripts/jenkins_lab.sh up; else absent "Jenkins A/B + git server" "scripts/jenkins_lab.sh up"; fi; fi
  if running netci-prod-host; then present "prod host 172.17.0.60"; else
    if up; then creating "prod host"; bash scripts/lab/prod_host.sh up >/dev/null; else absent "prod host" "scripts/lab/prod_host.sh up"; fi; fi
}

# ------------------------------------------------------------------ profile, keys, migrations
step_profile() {
  if [ -f "$GATE/keys/workload-token-keys" ]; then present "workload token keys"; elif up; then
    creating "workload token keys"; printf 'k1:%s\n' "$(rand 32)" > "$GATE/keys/workload-token-keys"; chmod 600 "$GATE/keys/workload-token-keys"; else absent "workload token keys"; fi
  if [ -f "$GATE/keys/cosign.pub" ]; then present "cosign key pair"; else absent "cosign key pair" "created by scripts/jenkins_lab.sh up"; fi
  if [ -x "$GATE/bin/cosign" ]; then present "cosign 3 binary"; elif up && running netci-git-server; then
    creating "cosign 3 binary (from the toolbox image)"; mkdir -p "$GATE/bin"; docker cp netci-git-server:/usr/local/bin/cosign "$GATE/bin/cosign"; else absent "cosign 3 binary" ".netci-gate/bin/cosign"; fi
  if [ -f "$GATE/ansible/ssh/id_ed25519" ]; then present "ansible ssh key"; elif up; then creating "ansible ssh key"; ssh-keygen -q -t ed25519 -N "" -f "$GATE/ansible/ssh/id_ed25519"; else absent "ansible ssh key"; fi
  if [ -d "$GATE/collections/ansible_collections" ]; then present "ansible collections"; elif up; then
    creating "ansible collections"; ansible-galaxy collection install -r deploy/ansible/requirements.yml -p "$GATE/collections" >/dev/null; else absent "ansible collections" "ansible-galaxy … -p .netci-gate/collections"; fi
  if [ -f "$PROFILE" ]; then present "profile .netci-gate/real-local.env"; else
    if up; then
      creating "profile"
      local secret pw
      secret="$(python3 -c "import json;print(next(c['secret'] for c in json.load(open('$GATE/keycloak/netci-realm.json'))['clients'] if c['clientId']=='netci'))")"
      pw="$(python3 -c "import json;print(json.load(open('$GATE/keycloak/netci-realm.json'))['users'][0]['credentials'][0]['value'])")"
      sed -e "s#__ROOT__#$ROOT#g" -e "s/__JENKINS_ADMIN_PASSWORD__/${JENKINS_ADMIN_PASSWORD:-change-me-local-only}/" \
          -e "s/__OIDC_CLIENT_SECRET__/$secret/" -e "s/__LAB_USER_PASSWORD__/$pw/" -e "s#__BACKUP_ENCRYPTION_KEY__#$(rand 32)#" \
          infra/lab/real-local.env.template > "$PROFILE"; chmod 600 "$PROFILE"
    else absent "profile" "rendered from infra/lab/real-local.env.template"; fi
  fi
  if [ -f "$PROFILE" ] && running netci-p0-pg; then
    local status; status="$(set -a; source "$PROFILE"; set +a; .venv/bin/python scripts/migrate.py --status 2>&1 | tail -1)"
    case "$status" in
      "up to date"*) present "migrations" "$status";;
      *) if up; then creating "migrations"; (set -a; source "$PROFILE"; set +a; .venv/bin/python scripts/migrate.py | tail -1); else absent "migrations" "$status"; fi;;
    esac
  fi
}

# ------------------------------------------------------------------ processes
step_services() {
  for spec in "api-a:8100" "api-b:8101"; do
    local name="${spec%%:*}" port="${spec##*:}"
    if curl -sf --max-time 3 "http://127.0.0.1:$port/healthz" >/dev/null; then present "API $name :$port"; elif up; then
      creating "API $name"; nohup scripts/lab/api_replica.sh "$name" "$port" > "$GATE/$name.log" 2>&1 & sleep 1; else absent "API $name :$port" "scripts/lab/api_replica.sh $name $port"; fi
  done
  local workers; workers="$(pgrep -fc "[p]ython -m app.workflows.worker" || true)"
  if [ "${workers:-0}" -ge 2 ]; then present "Temporal workers" "$workers"; elif up; then
    creating "Temporal workers"; for i in 1 2; do nohup scripts/lab/worker.sh > "$GATE/worker-$i.log" 2>&1 & done; else absent "Temporal workers" "${workers:-0} running; need 2 (scripts/lab/worker.sh)"; fi
  if running netci-prometheus && running netci-alertmanager; then present "Prometheus + Alertmanager"; elif up; then creating "monitoring"; bash scripts/lab/monitoring.sh up >/dev/null; else absent "Prometheus + Alertmanager" "scripts/lab/monitoring.sh up"; fi
  if systemctl --user is-enabled netci-backup.timer >/dev/null 2>&1; then present "backup + DR-drill timers"; elif up; then creating "backup timers"; bash scripts/lab/backup_timer.sh >/dev/null; else absent "backup + DR-drill timers" "scripts/lab/backup_timer.sh"; fi
}

step_verify() {
  if up || [ "$MODE" = "--check" ]; then
    for port in 8100 8101; do
      local body; body="$(curl -sf --max-time 20 "http://127.0.0.1:$port/readyz" 2>/dev/null || echo '{}')"
      local verdict; verdict="$(printf '%s' "$body" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('status','unreachable'), ' '.join(k for k,v in d.items() if isinstance(v,dict) and v.get('ready') is False))" 2>/dev/null || echo unreachable)"
      case "$verdict" in ok*) present "/readyz :$port" "ok";; *) absent "/readyz :$port" "$verdict";; esac
    done
  fi
}

step_prereqs; step_postgres; step_registry; step_temporal; step_keycloak; step_netbox; step_kind; step_jenkins; step_profile; step_services
if up && [ "$missing" -gt 0 ]; then sleep 15; fi
step_verify
echo
if [ "$missing" = 0 ]; then echo "everything present"; else echo "$missing item(s) missing or not ready"; exit 1; fi
