# ADR-085: Active-active Jenkins is installed by one chart; backups go through the Barman Cloud plugin

- Status: accepted; the lab now runs from the chart (measured below)
- Date: 2026-10-15
- Refines: ADR-076..084

## Context

Until now the lab was a set of manifests applied by hand in order (`lab/aa/phase2.yaml`,
`phase3.yaml` with digests substituted, `phase5.yaml`, `store.yaml`, `queue.yaml`), plus patches from
`store.sh`. A site cannot install from that, and nothing checked that a change to one manifest still
matched the others. The site's answers decide the variants: the VIP may come from Octavia or from
kube-vip; backups go to S3-compatible storage (Ceph RGW or MinIO); cluster-scoped operators are
allowed.

The store's backups used CloudNativePG's in-tree `barmanObjectStore`. It is deprecated and is
removed in 1.31, so a site that upgrades the operator loses its backups.

## Decision

1. **One chart, `deploy/helm/netci-jenkins-ha`**, renders everything the lab ran:
   - the replicas;
   - homesync;
   - the balancer and its config;
   - the VIP;
   - the CNPG store and its backups;
   - netci-queue;
   - the fence's RBAC.

   The names stay the lab's (`jenkins`, `homesync`, `haproxy`, `ui`, `store`, `queue`), because the
   plugin's configuration and `haproxy.cfg` refer to them. Everything that depends on the number of
   replicas is generated from `replicas`: replica URLs, cookies, `server` lines and agent homes.
2. **It refuses rather than guesses:**
   - every image must be pinned by digest;
   - the store's image must also carry a tag, because CNPG reads the major version from it;
   - an agent's home must be a replica that exists;
   - every Secret the replicas need must be named;
   - the fence must be one of `kubernetes` or `kubernetes-power`.

   The chart creates no Secret. Each is named in values and made by the site.
3. **VIP in two modes.**
   - `kubevip`: an ARP VIP. On OpenStack the port needs allowed-address-pairs for that address.
   - `loadBalancer`: a `LoadBalancer` Service with the site's annotations, which is Octavia through
     cloud-provider-openstack.

   Both use `externalTrafficPolicy: Local`, so the client's address reaches HAProxy and no hop goes
   through another machine.
4. **Backups through the Barman Cloud plugin** (`backup.mode: plugin`):
   - an `ObjectStore` with the same S3 settings;
   - the Cluster's `plugins` entry as WAL archiver;
   - `method: plugin` on the ScheduledBackup.

   `inTree` remains for a CNPG older than the plugin.
5. **The plugin is on the store's failover path, so it runs like the operator:**
   - two pods on two machines, with the release's leader election;
   - the 10 s not-ready/unreachable tolerations;
   - each pod uses its own machine's API server;
   - the readiness check runs every 2 s instead of 10 s.

   It is updated by Recreate, as the release ships it, because only the leader serves: a rolling
   update would wait forever for the new pod to become Ready.

## Measured in the lab

- **The chart against the live lab.**
  - `kubectl diff` of the render showed only labels, two pull secrets, and the homesync/queue image
    meant to change.
  - Helm then adopted the lab's 22 objects (`lab/aa/chart.sh adopt`) and applied the chart under
    the upgrade probe (`aa-phase6-upgrade-20261014T175148Z.json`): 219 of 219 UI reads, 220 of 220
    creates, none missing on either replica.
- **Switching the live store to the plugin through the chart** (`aa-phase6-upgrade-20261014T175428Z.json`).
  - The instances restarted one at a time with the plugin's sidecar, the primary last by
    switchover.
  - The UI answered 602 of 602 reads. Of 584 creates, 573 were accepted and none of those is missing.
  - The 11 refused all fell in the ~11 s of the switchover: 503, or 403 from the crumb problem (below).
- **Backup and restore through the plugin.** `store.sh backup` took a base backup.
  `store.sh restore-test` recovered a new cluster from it in 211 s: 19,192 items and 127 build
  numbers, identical to the store. The WAL it replayed was written partly in-tree, partly by the
  plugin.
- **The plugin found on the failover path.**
  - With its one pod stopped, the operator reconciled nothing for the cluster ("cannot proceed to
    reconciliation due to an error while interacting with plugins"). A deleted standby was not
    recreated, and the deleted primary was not replaced for 189 s. Started again, there was a
    primary 16 s later.
  - The primary's machine powered off (`phase6_store_probe.py --kill primary`), nothing lost in any
    run. Edits accepted again after:

| Plugin | Edits accepted again after | Evidence |
|---|---|---|
| none (in-tree, ADR-080) | 27.7 s, 42.1 s | `aa-phase6-store-20261014T045629Z`, `…043727Z` |
| two pods, leader on another machine | 38.7 s | `aa-phase6-store-20261014T182757Z` |
| two pods, leader on the primary's machine, readiness every 10 s | 73.8 s | `aa-phase6-store-20261014T182323Z` |
| the same, readiness every 2 s | 44.5 s | `aa-phase6-store-20261014T183829Z` |

  With the leader on the primary's machine, the operator kept calling the dead pod through the
  Service until the other pod took the lease (15 s) and passed its readiness check. Each row is one
  run.

## Rejected

- **Keeping the in-tree backups.** Rejected because CNPG 1.31 removes them.
- **A `pg_dump` CronJob instead of WAL archiving.** It needs no plugin, but it restores only to the
  last dump. A day of build numbers and edits would be gone.
- **One plugin pod.** That makes one machine's loss a store with no failover until Kubernetes moves
  the pod (minutes).
- **Each manifest kept beside the chart.** Rejected because two sources of one install drift apart.
  The phase manifests stay as the record of how the lab was built; `lab/aa/chart.sh` is how it is
  run now.

## What it costs, said plainly

- **cert-manager and the plugin are two more cluster-wide components.** While the plugin cannot be
  reached, the store does not fail over.
- **Placing the plugin away from the store.** Where the cluster has machines that run no store
  instance, the plugin belongs there. Then no store machine's loss takes its leader with it.
- **Upgrading the plugin** (Recreate) leaves a few seconds with no failover. Upgrade it when no
  machine is being worked on.
- **Crumbs on failover (ADR-077's open item).** A replica that refused an edit with 503 had HAProxy
  retry it on the other replica, where a browser session's crumb was not valid: 403 instead of "try
  again". Fixed in ADR-086 (controller netci31). Clients using an API token (the IDP, netci-queue)
  send no crumb.
