# netCI Helm Charts

This directory contains two Helm charts for installing netCI components:
1. `netci`: Installs the core platform (supervisor, queue, fabric).
2. `netci-cell`: Installs a Jenkins cell managed by netCI.

## `netci` Chart

The `netci` chart installs the platform services. Note that this chart installs one instance per namespace (resource names are fixed).

### Prerequisites

Before installing the `netci` chart, the following Secrets and ConfigMaps must exist:
- **Supervisor**: A Secret for fence credentials (e.g., `netci-fence` containing `id_ed25519` and `host_key.pub`, or a single config file).
- **Queue**: A Secret for the database URL (e.g., `netci-queue-db` containing the key `url`), and a Secret for cell credentials.
- **Fabric**: A Secret for the database URL (e.g., `netci-queue-db` containing the key `url`).

### Example Installation

```bash
helm install netci ./netci -n netci-system \
  --set image.repository=my-repo/netci \
  --set image.digest=sha256:yourdigest... \
  --set supervisor.fence.secretName=netci-fence \
  --set queue.database.secretName=netci-queue-db \
  --set queue.cellCredentials.secretName=netci-queue-cells \
  --set fabric.database.secretName=netci-queue-db
```

## `netci-cell` Chart

The `netci-cell` chart installs a single Jenkins cell configured for netCI, running under the cell supervisor's lease.

### Prerequisites

The controller image must be built from `jenkins/Dockerfile.controller`: it carries the netCI
plugin and the declared plugin set. A stock Jenkins image has neither.

Before installing the `netci-cell` chart, the following must exist:
- A ConfigMap containing the Jenkins Configuration as Code (JCasC) definition (e.g., `jenkins.yaml`).
- A Secret containing the cell's secrets (e.g., `admin-password`, `netci-password`, `fabric-token`,
  `once-token`). With a `casc-reload-token` key (any random string), a change to the JCasC
  ConfigMap is applied to the running controller within about a minute (the kubelet's update,
  then the cell agent's 10 s check), with no restart. Without it, the change applies at the
  controller's next start.

### Example Installation

```bash
helm install cell-a ./netci-cell -n cell-a \
  --set image.controller=registry.example/netci/jenkins-controller:2.555.3-netci5 \
  --set netci.repository=my-repo/netci \
  --set netci.digest=sha256:yourdigest... \
  --set casc.configMapName=jenkins-casc \
  --set secrets.secretName=jenkins-cell \
  --set storage.className=my-storage-class
```
