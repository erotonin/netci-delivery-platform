# ADR-096: A namespace per project, live on the HA lab

- Status: accepted; run live on the HA lab (below)
- Date: 2026-10-15
- Refines: ADR-030 (per-project build isolation), ADR-091 (the IDP on the replicas)

## Context

ADR-030 gives each application its own build namespace:
- an agent account without a token;
- the controller's rights on pods there and nowhere else;
- a NetworkPolicy with no ingress;
- a persistent cache claim.

It had never run on a cluster where builds pull from a private registry with its own CA. The HA
lab's IDP ran with `NETCI_BUILD_ISOLATION=none`, so every project's build shared `aa-build`.
Switching isolation on found four gaps.

1. **A pod in a new namespace could neither pull nor trust.** The cloud's pod template named a
   pull secret and a CA ConfigMap that only existed in `aa-build`.
2. **The account netCI provisions namespaces with has to create Secrets and Roles, cluster-wide.**
   RBAC cannot limit that to `netci-build-*` names, and `kubectl apply` also reads before it writes,
   which would mean reading every Secret.
3. **The project's cache held the git mirror, but not the image layers.** The toolbox's own
   `storage.conf` names a graphroot in the home directory, so `XDG_DATA_HOME` on the claim was
   ignored. Every build pulled the base image again and copied 17 blobs.
4. **The builder's AppArmor profile had only been loaded, not installed.** After `netci-lab-1`'s
   power cut, every build pod there failed: "apparmor profile not found". The build waited queued
   until the profile was back.

## Decision

1. **The provisioner writes what a build pod needs.**
   - `NETCI_BUILD_TRUSTED_CA_FILE` becomes the ConfigMap `netci-trusted-ca`.
   - `NETCI_BUILD_PULL_SECRET_FILE` becomes the Secret `netci-pull`, set as the agent account's
     image pull secret, so the pod template names none.
   - A file that is named but unreadable, or not what it should be, stops startup.
   - The Secret is applied server-side, so the account needs create and patch, never read.
   - kubectl's words are never repeated for a refused Secret: they can quote its data.
2. **The provisioner's account is fenced by admission, not only by RBAC**
   (`lab/aa/build-isolation.yaml`).
   - A ValidatingAdmissionPolicy refuses every write of
     `system:serviceaccount:netci-deploy:netci-build-provisioner` outside `netci-build-*`.
   - Its `escalate` and `bind` rights, needed to give the controller pod rights, therefore apply
     only there.
   - `lab/aa/idp.sh isolation-prepare` checks that the fence refuses a write in `default`, and
     fails otherwise.
3. **Buildah's storage is on the claim.** The cache pod sets `XDG_CONFIG_HOME=/netci-cache/config`
   (the variable rootless Buildah reads, ADR-092). Prepare Cache writes there a `storage.conf`
   whose graphroot is on the claim, keeping the toolbox's options; the runroot stays in the pod.
4. **The AppArmor profile is installed in `/etc/apparmor.d`**, where boot loads it again. The
   profile's header and the production guide say so.

## Measured on the lab

Evidence: `lab/evidence/aa-idp-isolation-20261015T121942Z.json`.

- **The namespace.** `netci-build-payments-api-c452cc` is labelled with the application and Pod
  Security `baseline`. It holds `jenkins-agent` (no token, pull secret attached), the Role and
  RoleBinding for `aa-p2/jenkins`, the NetworkPolicy `build-pods` (ingress none), a 2 Gi
  `netci-cache` on `local-path` (Bound), `netci-trusted-ca` and `netci-pull`. The build pod ran
  there as `jenkins-agent` with the claim mounted.
- **Builds.** payments-api built, signed and passed policy there, five times.
- **Cache.**

  | Build | Base image pulls | Layers from cache | Build stage |
  |---|---|---|---|
  | #30, before decision 3 | 1 | 0 | 10.0 s |
  | #31, first after | 1 | 0 | 8.5 s |
  | #32 | 0 | 6 | 6.1 s |

  The git mirror was a miss once (#29), then hit.
- **Ingress.** A listener in the project's namespace was refused from `aa-build` and from its own
  namespace. kube-router rejects rather than drops, so curl exited 7. The same listener in
  `aa-build` answered 200.
- **Fence.** The provisioner's account was refused creating a ConfigMap in `default`:
  "netci-build-provisioner writes only in netci-build-* namespaces".

## What it costs, said plainly

- **Egress stays open.** A build fetches from git, the registry and netCI, and pinning those
  addresses is the cluster operator's baseline policy (ADR-030).
- **The cache is one RWO claim per project on `local-path`.** It ties the project's builds to the
  node that holds the claim. A site with network storage chooses that class instead.
- **A build whose pod cannot start waits queued.** The IDP shows it queued; nothing times it out.
  A missing AppArmor profile is the kind of node fault that needs an alert, not a silent wait.
- **Builds on static agents (ADR-092) are in no namespace.** Startup refuses static agents
  together with this isolation.
