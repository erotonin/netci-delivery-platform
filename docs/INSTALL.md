# Installing netCI in production

This is the path the lab itself now runs: both Helm charts, installed by
`lab/helm-install.sh`. What was checked, and where, is stated with each step. Everything was
verified on k3s v1.36.4 with Longhorn 1.13 and Jenkins 2.555.3. Anything else is untested.

## 1. What you need first

| | Requirement | Why |
|---|---|---|
| Kubernetes | 1.29 or later (native sidecars, the `out-of-service` taint); 1.36 or later to use the Longhorn admission policies | The cell agent is a native sidecar; fencing relies on non-graceful node shutdown |
| Machines | Cells' machines with room for one more controller each (N+1): a takeover needs a machine that can take the controller | `NetciCellWithoutHeadroom` says when this is no longer true |
| Control plane | On machines that run no cells, if you can | A lost machine that also runs etcd's leader or the controllers' leaders adds seconds to every takeover (ADR-066) |
| Power control | A BMC per cell machine reachable from the cluster over Redfish, with an account allowed only to read and set power; or an SSH power agent for VMs (`lab/fence/`, installed by `lab/supervisor.sh`) | Nothing is fenced unless its power controller confirms the machine is off |
| Untrusted builds (optional) | Kata Containers on the machines that take them, which needs hardware or nested virtualisation (`lab/kata.sh` installs kata-deploy 4.2.0 with one shim) | A fabric pool with `runtimeClass: kata-qemu-runtime-rs` runs each build in its own VM; the fabric refuses to start if the class is missing |
| Storage | A `ReadWriteOnce` storage class that replicates across machines and attaches elsewhere once a node is out of service (Longhorn has been tested) | `JENKINS_HOME` moves with the controller |
| PostgreSQL | 14 or later, with backups; one database for netci-queue and netci-fabric | Runs, `netciOnce` markers and sandboxes are durable state |
| Registry | One that serves the netCI image and your controller image, pinned by digest | The charts refuse an image without a digest |
| Monitoring | prometheus-operator, Grafana's dashboard sidecar | Alerts and the dashboard (`monitoring.enabled`, `monitoring.dashboard.enabled`). Optional to install, but nothing tells a person about a failure the supervisor will not act on without it. Checked with kube-prometheus-stack 87.10.1 |

## 2. Images

- **netCI**: `make image` on a clean tree, push it, and note its digest. It holds every binary
  (supervisor, queue, fabric, cell agent, sandbox).
- **Your controller**: Jenkins with your plugins plus the netCI plugin. `make plugin` builds
  `jenkins/plugin/target/netci.hpi`. `jenkins/Dockerfile.controller` shows how to add it to
  a controller image: copy it as `netci.jpi.override`, so that an existing `JENKINS_HOME` gets
  the new version too.

## 3. The platform (chart `deploy/helm/netci`)

Secrets first. The chart creates none:
- `netci-queue-db`: key `url`, the PostgreSQL URL.
- The power controllers.
  - **Redfish:** one Secret with `fence.json` (format in `internal/fence/config.go`), plus a
    username, a password and a `tls-sha256` (or a `ca.crt`) for each BMC. Map its keys to the
    paths that `fence.json` names with `supervisor.fence.items`. `lab/redfish.sh secret`
    prints a complete example.
  - **SSH agent:** a Secret with `id_ed25519` and `host_key.pub`, plus `supervisor.machines`,
    `supervisor.fence.addr` and `supervisor.fence.user`.
- `netci-queue-cells`: for each cell, the user and API token that netci-queue uses to hand runs
  to that cell's controller.

Values: `deploy/helm/netci/ci/lab-values.yaml` is a complete example. Configuration holds token
**hashes** only:
- `queue.config`: cells, routes, clients, hooks. A client gets `jobs` to trigger runs and
  `once: true` to record `netciOnce` markers; give each controller a client with `once` only.
- `fabric.config`: pools, and the cells allowed to claim sandboxes.
- `queue.ingress`: HTTPS for webhooks. The chart refuses an ingress without a TLS secret.

```bash
helm upgrade --install netci deploy/helm/netci -n netci-system --create-namespace \
  -f my-values.yaml --set image.repository=REGISTRY/netci/netci --set image.digest=sha256:...
```

On Longhorn, apply its tuning policy and the StatefulSet exclusion as described in
`deploy/helm/README.md`. Without them, a takeover can wait 45 s for Longhorn to notice that an
API server was lost, and Longhorn may delete the new controller.

## 4. A cell (chart `deploy/helm/netci-cell`), one release per namespace

- **A JCasC ConfigMap** (`casc.configMapName`, key `jenkins.yaml`). Besides your own
  configuration, it needs:
  - a user for netci-queue's dispatcher;
  - `unclassified.netci` (`queueUrl`, `tokenFile: /run/secrets/cell/once-token`) for
    `netciOnce`;
  - a `netci` cloud for fabric sandboxes (`fabricUrl`, `tokenFile`, `controllerUrl`, pools), if
    you use the fabric.

  `lab/spike/cell.yaml` has a working example.
- **A Secret** (`secrets.secretName`), mounted at `/run/secrets/cell`: the passwords your JCasC
  reads, `fabric-token`, `once-token`, and optionally `casc-reload-token`, which applies JCasC
  changes without a restart.
- **Values**: `image.controller`, `netci.repository`/`netci.digest`, `storage.className`, `ui.ingress`
  (or `ui.nodePort`), `kubernetesPlugin.enabled` if builds use the Kubernetes plugin.

```bash
helm upgrade --install cell-payments deploy/helm/netci-cell -n cell-payments --create-namespace -f cell-values.yaml
```

**Moving a cell installed by hand to the chart.** First check that the StatefulSet's immutable
fields (`selector`, `serviceName`, `volumeClaimTemplates`) match the chart's. Then label and
annotate its objects as Helm's, and install over them. `lab/helm-install.sh` does exactly
this, and the cells kept their volumes.

## 5. Check it before anyone depends on it

1. `kubectl -n netci-system get lease netci-supervisor`: a replica leads. Its log says
   `node-to-machine mapping checked against the power controller`.
2. `netci_supervisor_cell_headroom` is 1 for every cell.
3. In Prometheus, every netCI target is up (supervisor, queue and fabric replicas, one cell agent
   per cell), and the `netci-*` rule groups are healthy. To see an alert end to end, scale
   `netci-supervisor` to 0: `NetciSupervisorNotLeading` fires within two minutes (96 s on the
   lab). Scale it back.
4. A build on each cell, and a run submitted through netci-queue. On the lab: a Kubernetes
   plugin build, a fabric build, a `netciOnce` build, and a queued run that finished in 24 s.
5. **A failure drill**: power off a cell's machine while a build runs, and, separately, hang one
   (`lab/spike/chaos_poweroff.py --failure node-poweroff-supervised|node-hang-supervised` shows
   how); check that the build finishes and that the hung machine was powered off by the
   supervisor. Do it on your hardware, through your power controllers, before production.

## 6. Running it

- `docs/RUNBOOK.md`: every alert, how to confirm it, what to do.
- **Upgrades**: `helm upgrade` with the new digest. The supervisor is replaced one replica at
  a time. A cell's controller restarts once, and Pipeline builds resume.
- **Backups**: PostgreSQL (runs, markers, sandboxes) with your usual tooling, and
  `JENKINS_HOME` through your storage's snapshots or backups. netCI copies neither.
- **Uninstalling**: `helm uninstall` per release. PVCs and the Secrets you created stay.
