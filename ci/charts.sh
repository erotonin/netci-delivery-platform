#!/usr/bin/env bash
# Lint and render both Helm charts, and check that they refuse what they must (CI, and by hand).
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
D="sha256:$(printf '0%.0s' $(seq 64))"
P=(-f "${ROOT}/deploy/helm/netci/ci/lab-values.yaml" --set image.digest="${D}")
C=(-f "${ROOT}/deploy/helm/netci-cell/ci/lab-values.yaml" --set netci.digest="${D}")
helm lint "${ROOT}/deploy/helm/netci" "${P[@]}"
helm lint "${ROOT}/deploy/helm/netci-cell" "${C[@]}"
helm template n "${ROOT}/deploy/helm/netci" -n netci-system "${P[@]}" --set monitoring.enabled=true >/dev/null
helm template c "${ROOT}/deploy/helm/netci-cell" -n cell-a "${C[@]}" --set kubernetesPlugin.enabled=true \
  --set logShipping.enabled=true --set logShipping.lokiUrl=http://loki:3100 \
  --set logShipping.image=cr.fluentbit.io/fluent/fluent-bit@sha256:c5542543523c9678398dd78d927c05e8425ec15b038f226b67b3b019b1a70845 >/dev/null
refuses() {  # refuses <what> <expected message> helm args...
  local what=$1 want=$2; shift 2
  if out=$(helm template x "$@" 2>&1); then echo "FAIL: rendered $what" >&2; exit 1; fi
  grep -q -- "${want}" <<<"${out}" || { echo "FAIL: $what refused for another reason: ${out}" >&2; exit 1; }
  echo "ok: refuses $what"
}
refuses "the platform without a digest" "digest" "${ROOT}/deploy/helm/netci" -f "${ROOT}/deploy/helm/netci/ci/lab-values.yaml" --set image.digest=
refuses "a cell without a digest" "netci.digest" "${ROOT}/deploy/helm/netci-cell" -f "${ROOT}/deploy/helm/netci-cell/ci/lab-values.yaml" --set netci.digest=
refuses "a toleration that is not a number" "outOfServiceTolerationSeconds" "${ROOT}/deploy/helm/netci-cell" "${C[@]}" --set outOfServiceTolerationSeconds=soon
refuses "log shipping without a pinned image" "pinned by digest" "${ROOT}/deploy/helm/netci-cell" "${C[@]}" --set logShipping.enabled=true --set logShipping.lokiUrl=http://loki:3100 --set logShipping.image=fluent-bit:latest
# The active-active Jenkins chart (ADR-085): both values files render; it refuses an image by tag,
# a store image without its tag, an agent homed on a replica that does not exist, a missing Secret.
H="${ROOT}/deploy/helm/netci-jenkins-ha"
for v in lab production; do
  helm lint --strict "$H" -f "$H/ci/$v-values.yaml"
  helm template h "$H" -n jenkins -f "$H/ci/$v-values.yaml" >/dev/null
done
helm template h "$H" -n jenkins -f "$H/ci/lab-values.yaml" --set store.cnpg.backup.mode=inTree --set balancer.vip.mode=loadBalancer >/dev/null
refuses "a controller by tag" "pinned by digest" "$H" -f "$H/ci/production-values.yaml" --set images.controller=jenkins/jenkins:lts
refuses "a store image without its tag" "major version from the tag" "$H" -f "$H/ci/production-values.yaml" --set images.postgres="r/postgres@${D}"
refuses "an agent homed on no replica" "must name a replica" "$H" -f "$H/ci/production-values.yaml" --set agents.homes.agent-9=jenkins-2
refuses "no session key Secret" "jenkins.sessionKeySecret" "$H" -f "$H/ci/production-values.yaml" --set jenkins.sessionKeySecret=
refuses "kube-vip without an address" "balancer.vip.address" "$H" -f "$H/ci/production-values.yaml" --set balancer.vip.mode=kubevip --set images.kubeVip="r/kube-vip@${D}"
refuses "a lenient fence by typo" "kubernetes-power" "$H" -f "$H/ci/production-values.yaml" --set jenkins.fence=power
refuses "a grace period the drain does not fit in" "at least drainSeconds" "$H" -f "$H/ci/production-values.yaml" --set jenkins.terminationGracePeriodSeconds=40
# ADR-089: the balancer asks each replica's drain agent, and the preStop waits for what is in flight.
helm template t "$H" -f "$H/ci/production-values.yaml" | grep -q "agent-check agent-port 8098" || { echo "FAIL: no agent check" >&2; exit 1; }
helm template t "$H" -f "$H/ci/production-values.yaml" | grep -q "netci-ready/busy" || { echo "FAIL: the preStop does not wait" >&2; exit 1; }
helm template t "$H" -f "$H/ci/production-values.yaml" --set jenkins.drainAgentPort=0 | grep -q "agent-check" && { echo "FAIL: an agent check with no agent" >&2; exit 1; }
echo "ok: the drain agent is asked, and only when there is one"
# The IDP's chart: it renders with the lab's values, and builds through netci-queue (ADR-082) need
# exactly one controller and the queue's token.
I="${ROOT}/deploy/helm/netci-platform"
IV=(-f "$I/examples/values-lab-kind.yaml")
ONE="$(mktemp)"; trap 'rm -f "$ONE"' EXIT
printf 'jenkins:\n  controllers: [{id: A, url: "http://vip", username: netci}]\n  runQueueUrl: http://queue:8080\n' > "$ONE"
Q=(-f "$ONE")
helm template p "$I" "${IV[@]}" >/dev/null
helm template p "$I" "${IV[@]}" "${Q[@]}" | grep -q NETCI_RUN_QUEUE_URL
refuses "the queue with two controllers" "exactly one entry" "$I" "${IV[@]}" --set jenkins.runQueueUrl=http://queue:8080
refuses "the queue without its token" "secrets.runQueueToken" "$I" "${IV[@]}" "${Q[@]}" --set existingSecret= \
  --set secrets.databaseUrl=postgresql://x --set secrets.workloadTokenKeys=k1:0123456789abcdef0123456789abcdef \
  --set secrets.cosignPublicKey=pub --set secrets.jenkinsApiTokens.A=t
echo "charts: ok"
