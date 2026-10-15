# ADR-092: The pipeline runs on a VM's inbound agents too

- Status: accepted; run live on the HA lab (below)
- Date: 2026-10-15
- Refines: ADR-091 (which listed this as not done), ADR-007 (one pod per build), ADR-079 (adoption)

## Context

The mentor's layout puts the controllers on Kubernetes and the build agents on one large OpenStack
VM, connected inbound. `netciPipeline` declared `agent { kubernetes { … } }` and nothing else, so on
that site the IDP's pipeline could not run at all. ADR-091 said so.

Declarative Pipeline fixes its `agent` when the script is parsed, so one pipeline cannot pick
between a pod and a label at run time. A second copy kept by hand would be 500 lines that drift
from the first. Every past fix to a stage (evidence, signing, callbacks) would have had to be made
twice.

## Decision

1. **The operator chooses where builds run, with `NETCI_AGENT_KIND`:**
   - `kubernetes` (the default) gives each build its own pod;
   - `static` runs builds on inbound agents labelled `NETCI_AGENT_LABEL` (the VM's).

   The value is written into the job netCI generates (`agentKind: '…'`), never into a build
   parameter, so no run, browser or webhook can change it. Any other value stops startup. `static`
   together with `NETCI_BUILD_ISOLATION=kubernetes` also stops startup: a static agent is in no
   per-project namespace, so the pair would claim an isolation no build had. The pipeline refuses
   the same combination again if a namespace parameter reaches it.
2. **The second pipeline is generated, not written.** `vars/netciPipelineOnAgents.groovy` is
   `vars/netciPipeline.groovy` with two changes:
   - the `netci:agent` block is replaced by `agent { label agentTemplate }`;
   - the `netci:dispatch` block is removed.

   `scripts/sync_shared_library.py` writes the copy and its `--check` fails CI when it is stale. A
   test asserts that both files have the same stages.
3. **On a long-lived agent each build gets its own image storage**, under the build's temporary
   directory, and removes it when it ends (`netciInBuilder`, `netciInBuilder.dropStorage()`).
   Rootless Buildah's default storage is in the agent's home. After the first build on the lab it
   held 233 MB, and every later build would have added to it until the VM's disk filled. It also
   let a later build of another module find this build's images already there, its base-image tag
   included. Pods never had this problem, because their storage dies with them.
   - The storage is set with `XDG_CONFIG_HOME`, because rootless Buildah 1.39 ignores
     `CONTAINERS_STORAGE_CONF` (measured).
   - The storage keeps the toolbox's `ignore_chown_errors`. Without it the first base image failed
     where `newuidmap` is denied ("insufficient UIDs or GIDs … requested 0:42").
   - The workspace is wiped too, by the `cleanWs` the pipeline already ran on agents that are not
     disposable.
4. **What the VM needs** is what the toolbox image (`jenkins/agent-toolbox/Dockerfile`) has:
   - an inbound agent with buildah, syft, trivy, cosign, git and python3;
   - the registry's CA in `/etc/containers/certs.d/<registry>/` and in `SSL_CERT_DIR`;
   - on Ubuntu 24.04, the builder's AppArmor profile (ADR-091), or user namespaces allowed for
     the agent's user.

   An active-active pair needs one agent per replica, each sent home by HAProxy (`agents.homes`):
   an inbound agent belongs to one controller.

## Measured on the HA lab

The lab's two inbound agents (`agent-0` and `agent-1`, which stand for the VM) were given the
toolbox image (`lab/aa/idp.sh vm-agents`) and the label `netci-vm`. The library on the lab's GitLab
was taken to `netci-0.5.3`.

| Run | Result |
|---|---|
| GitLab push faf3e7b, `NETCI_AGENT_KIND=static` | "Running on agent-0"; no pod in `aa-build`; all 9 stages; dev healthy (`aa-idp-e2e-20261015T062914Z.json`) |
| A second push (760cbe0) | Failed in Build: the first per-build storage lacked `ignore_chown_errors` (decision 3) |
| Retried after the fix | All 9 stages; dev healthy; afterwards the workspace was empty and the build's storage gone (`…063911Z`) |
| `NETCI_AGENT_KIND=kubernetes`, the same library | "Created Pod: netci-builds aa-build/…", build succeeded: the pod path still works (`…064106Z`) |

## What it costs, said plainly

- **A static agent is shared.** Builds on it are not isolated from each other the way pods are:
  separate workspaces and image storage are what keeps them apart, and a build that escapes them
  could affect the next one. A site that needs per-project isolation uses `kubernetes`.
- **Adoption is not yet shown with this pipeline.** ADR-079 proved that a build on an inbound agent
  survives its replica's death, but it proved it with a shell loop, not with the IDP's pipeline.
- **Two agents on one VM is not two machines.** If the VM is lost, every build on it is lost, and
  nothing in netCI can change that; it is the site's choice of one large VM.
