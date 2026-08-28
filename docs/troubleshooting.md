# netCI troubleshooting runbook

## Khởi động theo profile

| Profile | Bật | Dùng cho |
|---|---|---|
| `core` | API, PostgreSQL, Registry, MinIO, Jenkins A | Phát triển domain/CI |
| `k8s` | core + kind + Jenkins ephemeral agent | Kubernetes E2E |
| `container` | core + Docker VM | Docker/Ansible E2E |
| `systemd` | core + Systemd VM | Systemd/Ansible E2E |
| `full` | core + kind + Jenkins A/B + Temporalite | Demo tổng hợp, chạy tuần tự |

## Lỗi thường gặp

### API không lên

Kiểm tra port 8000, log container và health dependency. Không xóa database ngay; trước hết lưu evidence command/log.

```bash
docker compose ps
curl -fsS http://localhost:8000/healthz
```

### Jenkins JCasC không nạp

Kiểm tra `CASC_JENKINS_CONFIG`, file YAML có mount read-only, plugin JCasC đã cài và không có plaintext secret trong Git.

```bash
docker compose logs jenkins-a
 docker compose exec jenkins-a printenv CASC_JENKINS_CONFIG
```

### Ephemeral agent không tạo được

Kiểm tra Kubernetes context, namespace `netci-build`, service account/RBAC, image pull và resource limit. Không chuyển sang shared agent để che lỗi; ghi failure evidence trước.

### Helm deploy thất bại

Kiểm tra chart lint, namespace, image digest, kube context và readiness probe. Dùng `atomic: true` và `wait: true`; không dùng `replace: true` trong production path.

```bash
helm lint deploy/helm/sample-kubernetes-app
kubectl -n staging get pods,events
helm -n staging status hello-kubernetes
```

### Ansible SSH thất bại

Kiểm tra IP inventory, SSH key, host key, Python trên target và quyền sudo. Dùng `ansible-inventory --graph` và `ansible all -m ping` trước khi deploy.

### Systemd service không healthy

Kiểm tra binary architecture, permission, unit file, symlink `current`, port 18080 và journal.

```bash
systemctl status hello-systemd
journalctl -u hello-systemd -n 100 --no-pager
curl -fsS http://127.0.0.1:18080/healthz
```

### Security gate chặn artifact

Không bypass policy. Kiểm tra digest có immutable không, SBOM có tồn tại, Trivy report, signature và identity/key reference. Chỉ cho phép exception khi có audit record và approval đúng role.

### Build chậm sau khi dùng ephemeral agent

Phân tách queue, provisioning, cache restore, build và cleanup. Không kết luận chỉ từ tổng thời gian. So sánh cùng commit, cùng agent resource, cùng image và cùng cache policy.

### Controller A down

Router phải ngừng route build mới vào A, chọn B theo health/queue/capacity và ghi detection/reroute/completion timestamps. Build đang chạy trên A không được tự ý migrate nếu chưa có checkpoint/idempotency.


## Host constraints that are not obvious from the error

These cost real time to diagnose, because none of them reports itself as a missing
prerequisite. `python3 scripts/doctor.py --profile ubuntu` now checks the first two.

### The kind worker node never becomes Ready

The kubelet journal shows `inotify_init: too many open files` and nothing else. kubelet,
containerd and cAdvisor each open inotify instances, and on a host already running many
containers the default `fs.inotify.max_user_instances` (often 128) is exhausted:

```bash
sudo sysctl -w fs.inotify.max_user_instances=1024 fs.inotify.max_user_watches=524288
docker exec <cluster>-worker systemctl restart kubelet
```

Persist it in `/etc/sysctl.d/` if the lab is long-lived.

### Published container ports do nothing

If `/etc/docker/daemon.json` sets `"iptables": false` or `"bridge": "none"` (common on
hosts that manage their own networking, such as a Kolla/OpenStack node), Docker cannot
install the NAT rules that make `-p` work. User-defined networks still work at layer 2,
so kind is fine, but compose's published ports are not.

Use the host-network lab instead, which the gates support directly:

```bash
make lab-up NETCI_LAB_MODE=hostnet
make e2e-container NETCI_LAB_NETWORK_MODE=host
```

### The cluster cannot pull the image the host just pushed

Docker only treats `localhost`/`127.0.0.1` registries as insecure by default, while the
cluster cannot resolve `localhost` as the host. Run the registry on the host network and
map the reference host to the kind gateway through containerd's `certs.d`:

```bash
REGISTRY_MODE=gateway REGISTRY_PORT=55000 REGISTRY_PULL_HOST=localhost:55000 \
  bash infra/kind/local-registry.sh
```

Both sides then use the identical image reference, so the digest is the same on both.

### A pod that pulls its own image times out

A lab cluster usually has no outbound internet, so anything that pulls a fresh image from
Docker Hub at check time will hang. The Kubernetes gate reaches the Service through
`kubectl port-forward`, which tunnels over the API server connection that already works.

### Ansible fails with "Failed to import the required Python library"

`kubernetes.core` and `community.docker` need Python libraries in the *same interpreter*
`ansible-playbook` runs under:

```bash
pip install -r deploy/ansible/requirements.txt
ansible-galaxy collection install -r deploy/ansible/requirements.yml -p .netci-gate/collections
```

### A build fails the vulnerability gate on a base image you just pinned

That is the gate working. Pinning freezes a base at the vulnerabilities it shipped with;
once fixes are published, Trivy reports them as fixable and the policy refuses the
artifact. Rebuild on current packages (`apk upgrade` / `apt-get upgrade` in the
Dockerfile) rather than lowering the threshold. See [security model](security-model.md).

### Rootless buildah fails with `unshare(CLONE_NEWUSER): Operation not permitted`

Docker's default seccomp profile blocks `clone(CLONE_NEWUSER)` for unprivileged
containers, and on Ubuntu 24.04 `kernel.apparmor_restrict_unprivileged_userns=1` blocks it
a second time at the kernel. Rootless buildah needs a user namespace, so a build agent
running as a plain Docker container cannot build an image until both are relaxed:

```bash
sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0   # host-wide
docker run --security-opt seccomp=unconfined --security-opt apparmor=unconfined ...
```

Note which agent needs this. The **shared** agent in `scripts/jenkins_lab.sh` — the
benchmark baseline — is a Docker container and needs both grants. The **ephemeral** agent
is a Kubernetes pod, where the kubelet's RuntimeDefault profile already permits it and the
pod is destroyed after one build regardless. The long-lived agent is the one that has to be
handed a broader privilege *and* keeps it between builds; that asymmetry is an argument for
[ADR-007](decisions/ADR-007-ephemeral-agent-isolation.md), not an incidental lab detail.

### A change to the shared library or JCasC does not take effect

Controllers clone the shared library from the git server the lab publishes, and read
`jenkins/casc` through a read-only bind mount. Neither is re-read on its own:

```bash
bash scripts/jenkins_lab.sh publish
```

That re-snapshots the working tree and the library, and asks both running controllers to
reload their JCasC. A change to the controller image, or to the environment the container
was started with, needs `bash scripts/jenkins_lab.sh recreate <a|b>` instead.

### `createItem` or `buildWithParameters` returns 403 with a valid API token

The CSRF crumb Jenkins issues is bound to the session cookie it was issued with, so a
client that sends the crumb without the cookie is refused on every POST. The crumb also
dies when the controller restarts — which the rebuild gate does on purpose — so a cached
one has to be discarded and re-fetched on a 403. `scripts/netci_gates/jenkins.py` does
both; use `JenkinsClient` rather than writing another HTTP client.
