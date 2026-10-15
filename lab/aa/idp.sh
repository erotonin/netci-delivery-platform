#!/usr/bin/env bash
# The IDP's pipeline on the active-active replicas (ADR-091): what the lab needs beside the chart.
#
#   lab/aa/idp.sh ci-prepare   namespace aa-build (pull secret, lab CA), Secret ci-credentials in aa-p2
#   lab/aa/idp.sh casc         merge lab/aa/casc-ci.yaml into the lab's JCasC ConfigMap (applied at the
#                              replicas' next start: lab/aa/chart.sh upgrade, or a rolling restart)
#   lab/aa/idp.sh deploy-prepare  namespaces dev/staging/prod and the worker's own kubeconfig (aa-lab)
#   lab/aa/idp.sh vm-agents    the lab's inbound agents (agent-0/1, the mentor's VM) carry the build toolbox,
#                              for NETCI_AGENT_KIND=static (ADR-092)
#   lab/aa/idp.sh isolation-prepare  per-project build namespaces (ADR-096): the provisioner account, its
#                              admission fence, its kubeconfig and the pull secret file for the IDP
#   lab/aa/idp.sh env         write .netci-gate/aa/idp.env: the IDP against the replicas, through netci-queue
#   lab/aa/idp.sh api|worker   run the IDP's API (:18100, reachable by build pods) or its CD worker
#
# Credentials come from .netci-gate and go into the Secret through files in a private temporary
# directory: never printed, never on a command line, trailing newlines stripped (a token read by
# JCasC's ${readFile:...} keeps one, and the password would then be wrong).
set -euo pipefail
cd "$(dirname "$0")/../.."
export KUBECONFIG="${KUBECONFIG:-.netci-gate/lab/kubeconfig}"
NS=aa-p2
BUILD_NS=aa-build
C=.netci-gate/corp

ci_prepare() {
  kubectl get namespace "$BUILD_NS" >/dev/null 2>&1 || kubectl create namespace "$BUILD_NS" >/dev/null
  kubectl -n "$NS" get secret harbor-pull -o json \
    | python3 -c 'import json,sys; s=json.load(sys.stdin); m=s["metadata"]; s["metadata"]={"name":m["name"],"namespace":"'"$BUILD_NS"'"}; print(json.dumps(s))' \
    | kubectl apply -f - >/dev/null
  kubectl -n "$BUILD_NS" create configmap lab-ca --from-file=ca.crt="$C/pki/ca.crt" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
  # What every build namespace has (ADR-096): the CA as netci-trusted-ca, the pull secret on the agent's account.
  kubectl -n "$BUILD_NS" create configmap netci-trusted-ca --from-file=ca.crt="$C/pki/ca.crt" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
  kubectl -n "$BUILD_NS" patch serviceaccount jenkins-agent -p '{"imagePullSecrets":[{"name":"harbor-pull"}]}' >/dev/null
  local tmp; tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' RETURN
  chmod 700 "$tmp"
  python3 - "$tmp" <<'EOF'
import os, sys
out = sys.argv[1]
src = {
    "cosign-key": ".netci-gate/jenkins/secrets/NETCI_COSIGN_PRIVATE_KEY",
    "gitlab-token": ".netci-gate/corp/gitlab_jenkins_token",
    "harbor-robot-name": ".netci-gate/corp/harbor_robot_name",
    "harbor-robot-secret": ".netci-gate/corp/harbor_robot_secret",
}
for key, path in src.items():
    value = open(path).read()
    value = value if key == "cosign-key" else value.strip()
    fd = os.open(os.path.join(out, key), os.O_WRONLY | os.O_CREAT, 0o600)
    os.write(fd, value.encode()); os.close(fd)
EOF
  kubectl -n "$NS" create secret generic ci-credentials \
    --from-file=cosign-key="$tmp/cosign-key" --from-file=gitlab-token="$tmp/gitlab-token" \
    --from-file=harbor-robot-name="$tmp/harbor-robot-name" --from-file=harbor-robot-secret="$tmp/harbor-robot-secret" \
    --dry-run=client -o yaml | kubectl apply -f - >/dev/null
  echo "aa-build ready; ci-credentials in $NS"
}

casc() {
  kubectl -n "$NS" get configmap casc -o json | python3 -c '
import json, sys, yaml
cm = json.load(sys.stdin)
base = yaml.safe_load(cm["data"]["jenkins.yaml"])
extra = yaml.safe_load(open("lab/aa/casc-ci.yaml"))
def merge(a, b):
    for k, v in b.items():
        if isinstance(v, dict) and isinstance(a.get(k), dict):
            merge(a[k], v)
        else:
            a[k] = v   # lists replace: the clouds and libraries are this file'"'"'s alone
    return a
merged = merge(base, extra)
cm["data"]["jenkins.yaml"] = yaml.safe_dump(merged, sort_keys=False, width=200)
for k in ("resourceVersion", "uid", "creationTimestamp", "managedFields"):
    cm["metadata"].pop(k, None)
print(json.dumps(cm))
' | kubectl apply -f - >/dev/null
  echo "casc merged with lab/aa/casc-ci.yaml (the replicas read it at their next start)"
}

# The IDP's own profile for the HA lab, from the systemd lab's (.netci-gate/real-local.env): sign-in,
# separation of duties, evidence and signature policy, workload token keys and Ansible keys are kept;
# CI, registry, Temporal and the database are the HA lab's. Written with mode 600; never printed.
env_file() {
  python3 lab/aa/idp_env.py
}

run() {
  set -a; source .netci-gate/aa/idp.env; set +a
  export PYTHONPATH=backend NETCI_PROJECT_ROOT="$PWD"
  case "$1" in
    api) exec .venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 18100 --log-level info ;;
    worker) export NETCI_ANSIBLE_INVENTORY="${NETCI_ANSIBLE_INVENTORY:-$PWD/deploy/ansible/inventories/localhost.ini}"
            exec .venv/bin/python -m app.workflows.worker ;;
  esac
}

# Deployment targets: namespaces dev, staging and prod in the lab cluster, reached by the IDP's
# worker as netci-deployer -- admin in those three namespaces and nothing else -- through its own
# kubeconfig (.netci-gate/aa/kubeconfigs/aa-lab, the module's kubeconfigRef), never the lab's.
deploy_prepare() {
  kubectl get namespace netci-deploy >/dev/null 2>&1 || kubectl create namespace netci-deploy >/dev/null
  kubectl -n netci-deploy create serviceaccount netci-deployer --dry-run=client -o yaml | kubectl apply -f - >/dev/null
  for env in dev staging prod; do
    kubectl get namespace "$env" >/dev/null 2>&1 || kubectl create namespace "$env" >/dev/null
    kubectl -n "$NS" get secret harbor-pull -o json \
      | python3 -c 'import json,sys; s=json.load(sys.stdin); m=s["metadata"]; s["metadata"]={"name":m["name"],"namespace":sys.argv[1]}; print(json.dumps(s))' "$env" \
      | kubectl apply -f - >/dev/null
    # The sample chart names no pull secret; the namespace's default account carries it.
    kubectl -n "$env" patch serviceaccount default -p '{"imagePullSecrets":[{"name":"harbor-pull"}]}' >/dev/null
    kubectl -n "$env" create rolebinding netci-deployer --clusterrole=admin --serviceaccount=netci-deploy:netci-deployer \
      --dry-run=client -o yaml | kubectl apply -f - >/dev/null
  done
  kubectl -n netci-deploy apply -f - >/dev/null <<'EOF'
apiVersion: v1
kind: Secret
metadata:
  name: netci-deployer-token
  annotations: {kubernetes.io/service-account.name: netci-deployer}
type: kubernetes.io/service-account-token
EOF
  sleep 2
  mkdir -p .netci-gate/aa/kubeconfigs && chmod 700 .netci-gate/aa/kubeconfigs
  python3 - <<'EOF'
import base64, json, os, subprocess, yaml
k = lambda *a: subprocess.run(["kubectl", *a], check=True, capture_output=True, text=True).stdout
sec = json.loads(k("-n", "netci-deploy", "get", "secret", "netci-deployer-token", "-o", "json"))["data"]
lab = yaml.safe_load(open(os.environ["KUBECONFIG"]))
cfg = {"apiVersion": "v1", "kind": "Config", "current-context": "aa-lab",
       "clusters": [{"name": "aa-lab", "cluster": {"server": lab["clusters"][0]["cluster"]["server"],
                                                   "certificate-authority-data": sec["ca.crt"]}}],
       "users": [{"name": "netci-deployer", "user": {"token": base64.b64decode(sec["token"]).decode()}}],
       "contexts": [{"name": "aa-lab", "context": {"cluster": "aa-lab", "user": "netci-deployer"}}]}
path = ".netci-gate/aa/kubeconfigs/aa-lab"
fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
os.write(fd, yaml.safe_dump(cfg).encode()); os.close(fd)
print(f"wrote {path} (netci-deployer, namespaces dev/staging/prod)")
EOF
}

# The lab's two inbound agents stand for the mentor's large VM (phase5.yaml): one agent per replica,
# each sent home by HAProxy. For the IDP's pipeline on static agents (ADR-092) they run the build
# toolbox image -- an inbound agent with buildah, syft, trivy and cosign on PATH, what the VM would
# have installed -- under the builder's AppArmor profile, trusting the registry's CA.
vm_agents() {
  kubectl -n "$NS" create configmap lab-ca --from-file=ca.crt="$C/pki/ca.crt" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
  python3 - <<'EOF' | kubectl -n "$NS" patch statefulset agent --type strategic --patch-file /dev/stdin >/dev/null
import json, yaml
cloud = yaml.safe_load(open("lab/aa/casc-ci.yaml"))["jenkins"]["clouds"][0]["kubernetes"]
pod = yaml.safe_load(cloud["templates"][0]["yaml"])
toolbox = next(c["image"] for c in pod["spec"]["containers"] if c["name"] == "builder")
print(json.dumps({"spec": {"template": {"spec": {
    "imagePullSecrets": [{"name": "harbor-pull"}],
    "volumes": [{"name": "lab-ca", "configMap": {"name": "lab-ca"}}],
    "containers": [{
        "name": "agent", "image": toolbox,
        "env": [{"name": "BUILDAH_ISOLATION", "value": "chroot"}, {"name": "STORAGE_DRIVER", "value": "vfs"},
                {"name": "SSL_CERT_DIR", "value": "/etc/ssl/certs:/etc/netci/registry-ca"}],
        "securityContext": {"allowPrivilegeEscalation": True,
                            "appArmorProfile": {"type": "Localhost", "localhostProfile": "netci-builder"}},
        # Requests as before (netci-lab-3 is full); a build needs more than an idle agent.
        "resources": {"limits": {"memory": "2Gi"}},
        "volumeMounts": [
            {"name": "lab-ca", "mountPath": "/etc/containers/certs.d/172.17.0.1:8930/ca.crt", "subPath": "ca.crt", "readOnly": True},
            {"name": "lab-ca", "mountPath": "/etc/netci/registry-ca", "readOnly": True}]}]}}}}))
EOF
  kubectl -n "$NS" rollout status statefulset/agent --timeout=600s >/dev/null
  kubectl -n "$NS" get statefulset agent -o jsonpath='agents run {.spec.template.spec.containers[0].image}{"\n"}'
}

# Per-project build namespaces (ADR-030, ADR-096): the provisioner's account and its admission fence
# (lab/aa/build-isolation.yaml), its kubeconfig, and the pull secret the IDP writes into each namespace.
isolation_prepare() {
  kubectl apply -f lab/aa/build-isolation.yaml >/dev/null
  sleep 2
  mkdir -p .netci-gate/aa/kubeconfigs && chmod 700 .netci-gate/aa/kubeconfigs
  python3 lab/aa/isolation_files.py
  # The fence holds: the account is refused outside netci-build-*. A new policy takes a few seconds to
  # be enforced; a write that got through before then is removed and counted as a failure.
  local i out
  for i in $(seq 1 15); do
    out="$(kubectl --kubeconfig .netci-gate/aa/kubeconfigs/build-provisioner create configmap fence-check -n default \
      --from-literal=a=b 2>&1 || true)"
    if grep -q "netci-build-provisioner writes only" <<<"$out"; then
      echo "admission fence holds: refused in default"
      return 0
    fi
    kubectl -n default delete configmap fence-check --ignore-not-found >/dev/null
    sleep 2
  done
  echo "admission fence did NOT refuse a write in default" >&2
  return 1
}

case "${1:-}" in
  ci-prepare) ci_prepare ;;
  deploy-prepare) deploy_prepare ;;
  casc) casc ;;
  vm-agents) vm_agents ;;
  isolation-prepare) isolation_prepare ;;
  env) env_file ;;
  api|worker) run "$1" ;;
  *) sed -n '2,10p' "$0"; exit 2 ;;
esac
