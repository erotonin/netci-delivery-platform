#!/usr/bin/env bash
# Fail the single Jenkins controller over (ADR-055) and measure it.
#
#   scripts/corp/jenkins_failover.sh [--planned] [--no-marker]
#
# What it does, in the order a real node loss would force:
#   1. (drill) leaves a marker in JENKINS_HOME: a job with one finished build;
#   2. --planned: takes a backup now; otherwise uses the newest completed scheduled backup;
#   3. cordons the controller's node (it is "gone") and deletes the jenkins namespace --
#      StatefulSet, PVC and all -- because Velero's file-system restore writes the volume
#      data only into a pod *it* recreates; a pod the StatefulSet recreates would start on
#      an empty volume;
#   4. restores the namespace from the backup, which lands on another worker;
#   5. waits for Jenkins to answer, checks the marker build is back, prints RTO and RPO.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SECRETS="${ROOT}/.netci-gate/corp"
CONTEXT="${KUBE_CONTEXT:-kind-netci-corp}"
VELERO=("${SECRETS}/bin/velero" --kubecontext "${CONTEXT}")
K=(kubectl --context "${CONTEXT}")
PLANNED=false; MARKER=true
for arg in "$@"; do case "$arg" in --planned) PLANNED=true ;; --no-marker) MARKER=false ;; *) echo "unknown $arg" >&2; exit 2 ;; esac; done
log() { printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*"; }

jenkins_url=http://127.0.0.1:18089
forward() {
  "${K[@]}" -n jenkins port-forward svc/jenkins 18089:8080 --address 127.0.0.1 >/dev/null 2>&1 &
  FORWARD=$!
  for _ in $(seq 60); do curl -s -o /dev/null "${jenkins_url}/login" && return 0; sleep 2; done
  return 1
}
jenkins() {  # admin API call. The credential reaches curl as config on stdin (-K -), written
  # by the printf builtin: on a command line it would be readable in `ps` by any local user.
  printf 'user = "admin:%s"\n' "$(<"${SECRETS}/jenkins_admin_password")" | curl -K - -fsS "$@"
}

marker_build=""
if ${MARKER}; then
  forward
  crumb=$(jenkins -c /tmp/.jf-cookies "${jenkins_url}/crumbIssuer/api/json" | python3 -c 'import json,sys;d=json.load(sys.stdin);print(d["crumbRequestField"]+":"+d["crumb"])')
  job="dr-marker"
  jenkins -b /tmp/.jf-cookies -H "${crumb}" -H 'Content-Type: application/xml' -X POST "${jenkins_url}/createItem?name=${job}" \
    --data-binary '<flow-definition plugin="workflow-job"><definition class="org.jenkinsci.plugins.workflow.cps.CpsFlowDefinition" plugin="workflow-cps"><script>echo "failover marker"</script><sandbox>true</sandbox></definition></flow-definition>' >/dev/null 2>&1 || true
  # The number this trigger will get, read first: on a job that already has builds,
  # lastSuccessfulBuild answers with the *previous* one until the new one finishes, and a
  # drill that marks an old build proves only that old history survives.
  expected=$(jenkins "${jenkins_url}/job/${job}/api/json" | python3 -c 'import json,sys;print(json.load(sys.stdin)["nextBuildNumber"])')
  jenkins -b /tmp/.jf-cookies -H "${crumb}" -X POST "${jenkins_url}/job/${job}/build" >/dev/null
  for _ in $(seq 90); do
    last=$(jenkins "${jenkins_url}/job/${job}/lastSuccessfulBuild/api/json" 2>/dev/null | python3 -c 'import json,sys;print(json.load(sys.stdin)["number"])' 2>/dev/null || true)
    if [[ -n "${last}" ]] && (( last >= expected )); then marker_build="${last}"; break; fi
    sleep 2
  done
  [[ -n "${marker_build}" ]] || { echo "marker build #${expected} did not finish" >&2; exit 1; }
  kill "${FORWARD}" 2>/dev/null || true
  log "marker: ${job} build #${marker_build} is in JENKINS_HOME"
fi

if ${PLANNED}; then
  backup="planned-$(date +%Y%m%d-%H%M%S)"
  log "planned switch: taking backup ${backup}"
  "${VELERO[@]}" backup create "${backup}" --from-schedule jenkins-backup --wait >/dev/null
else
  if ${MARKER}; then
    # A drill must prove the marker survives, so it must be in the backup it restores.
    backup="drill-$(date +%Y%m%d-%H%M%S)"
    # --from-schedule: the schedule's own template, quiesce hook and file-system backup
    # included. A plain `backup create` carries neither, and would not be what failover
    # restores from in a real failure.
    "${VELERO[@]}" backup create "${backup}" --from-schedule jenkins-backup --wait >/dev/null
  else
    backup=$("${VELERO[@]}" backup get -o json | python3 -c '
import json,sys
d=json.load(sys.stdin); items=d.get("items",[d]) if "items" in d else [d]
ok=[i for i in items if i.get("status",{}).get("phase")=="Completed"]
print(max(ok,key=lambda i:i["status"]["completionTimestamp"])["metadata"]["name"] if ok else "")')
  fi
fi
[[ -n "${backup}" ]] || { echo "no completed backup of namespace jenkins to restore from" >&2; exit 1; }
phase=$("${VELERO[@]}" backup get "${backup}" -o json | python3 -c 'import json,sys;print(json.load(sys.stdin)["status"]["phase"])')
[[ "${phase}" == "Completed" ]] || { echo "backup ${backup} is ${phase}, not Completed" >&2; exit 1; }
backup_at=$("${VELERO[@]}" backup get "${backup}" -o json | python3 -c 'import json,sys;print(json.load(sys.stdin)["status"]["completionTimestamp"])')

start=$(date +%s)
node=$("${K[@]}" -n jenkins get pod jenkins-0 -o jsonpath='{.spec.nodeName}')
log "failure: controller node ${node} is cordoned (gone); deleting namespace jenkins"
"${K[@]}" cordon "${node}" >/dev/null
"${K[@]}" delete namespace jenkins --wait=true --timeout=180s >/dev/null

log "restore: namespace jenkins from ${backup}"
restore="restore-${backup}-$(date +%H%M%S)"
"${VELERO[@]}" restore create "${restore}" --from-backup "${backup}" --wait >/dev/null
rphase=$("${VELERO[@]}" restore get "${restore}" -o json | python3 -c 'import json,sys;print(json.load(sys.stdin)["status"]["phase"])')
log "restore ${restore}: ${rphase}"
"${K[@]}" -n jenkins wait --for=condition=ready pod/jenkins-0 --timeout=600s >/dev/null
new_node=$("${K[@]}" -n jenkins get pod jenkins-0 -o jsonpath='{.spec.nodeName}')
forward
end=$(date +%s)

restored=""
if ${MARKER}; then
  restored=$(jenkins "${jenkins_url}/job/dr-marker/lastSuccessfulBuild/api/json" 2>/dev/null | python3 -c 'import json,sys;print(json.load(sys.stdin)["number"])' 2>/dev/null || true)
fi
kill "${FORWARD}" 2>/dev/null || true
"${K[@]}" uncordon "${node}" >/dev/null

rpo=$(( start - $(date -d "${backup_at}" +%s) ))
log "controller moved ${node} -> ${new_node}"
log "RTO $(( end - start )) s (failure to Jenkins answering), RPO ${rpo} s (age of the backup restored)"
if ${MARKER}; then
  if [[ "${restored}" == "${marker_build}" && -n "${restored}" ]]; then
    log "PASS: marker build #${restored} survived the failover"
  else
    log "FAIL: marker build #${marker_build} not found after restore (got '${restored}')"; exit 1
  fi
fi
