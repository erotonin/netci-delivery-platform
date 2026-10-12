# netci.delivery

An Ansible collection containing reference playbooks for deploying one approved
immutable artifact to Docker Compose, Kubernetes/Helm or Systemd. Every path
validates the digest, performs a runtime health check and either confirms the
requested artifact is running or fails the deployment. Docker and Systemd also
attempt rollback when the new release fails its health gate.

Build the collection from the repository root:

```bash
ansible-galaxy collection build deploy/ansible
```

Install dependencies for source-tree development with:

```bash
python -m pip install -r deploy/ansible/requirements.txt
ansible-galaxy collection install -r deploy/ansible/requirements.yml
```

The checked-in inventory is local-lab material and is deliberately excluded
from the collection artifact. Production callers must provide their own
inventory, SSH host keys, private key and Kubernetes credential. Never accept
inventory hosts, secret paths or executable task content directly from a web
client.

`meta/execution-environment.yml` exposes the collection's controller-side
Python requirements to `ansible-builder`; the collection dependencies in
`galaxy.yml` install the Docker and Kubernetes content. A plain Galaxy install
does not install Python packages, so non-Execution-Environment users must also
install `requirements.txt`.

The netCI runtime invokes these playbooks from the source tree. The live gates
under `scripts/gate_e2e_*` are the authoritative verification until equivalent
collection integration tests are published.
