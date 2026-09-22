# ADR-040: netCI ships two ways to deploy itself, and neither starts on placeholders

Status: Accepted.

## Context

netCI deploys other people's modules. Until now it had no answer for how it is itself
installed on a customer's Kubernetes: the lab runs it as user-systemd units
(`netci-api-a`, `netci-api-b`, `netci-worker-1`, `netci-portal`), which is right for a
lab on one host and is not something anyone would hand to an operations team.

Two artefacts now exist for that: plain manifests under `deploy/k8s/netci-platform`
(namespace, RBAC, configmap, secrets example, backend, frontend, worker, ingress, with a
kustomization) and a Helm chart under `deploy/helm/netci-platform` covering the same
ground with values.

## Decision

**Both, deliberately.** They are not redundant:

- The **plain manifests** are readable. An operations team that has to approve what runs
  in a cluster reads nine files and sees exactly what will exist. `kubectl apply -k` needs
  no extra tool in the change window.
- The **Helm chart** is parameterised. A team already running Helm gets one release
  object, upgrades and rollbacks, and per-environment values files.

Keeping both costs a duplicated description of the same deployment. That cost is real and
is accepted, because the audience for the two is different and neither audience is served
well by the other's format.

**Neither renders without its secrets.** The chart marks every credential `required` and
carries no defaults (ADR-038 is about the same principle inside the application). A chart
that installs on placeholder values produces a control plane that looks configured and is
not -- and one of those placeholders was the workload-identity signing key, published in
this repository. `helm install` with no values file fails naming the value it wants.
The plain manifests ship `03-secrets.example.yaml`, which must be renamed before
`kubectl apply -k` will find it.

## What was rejected

**Only the Helm chart.** It assumes Helm in the change window and turns "what will this
create?" into a templating question. Some of the organisations this is written for do not
allow that.

**Only the plain manifests.** Every per-environment difference becomes a copied directory,
which is how manifest drift starts.

**Deploying netCI with netCI.** Circular at install time: the thing that would run the
pipeline is the thing being installed. It is worth revisiting for upgrades once an
installation exists, and it is not how the first one gets there.

## Consequences

- `deploy/helm/netci-platform` and `deploy/k8s/netci-platform` describe the same
  topology and have to be changed together; a difference between them is a defect.
- `helm lint deploy/helm/netci-platform` passes and is the cheap check.
- `docs/HUONG_DAN_DAU_NOI_JENKINS_THUC_TE.md` walks through both against a real Jenkins.
- Neither has been run against a real cluster from this environment, so nothing here is
  live-verified.
