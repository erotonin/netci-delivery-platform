# ADR-029: The registry is the only artifact store; durable server state lives in PostgreSQL

Status: Accepted.

## Context

Running the three golden paths on the live stack (ADR-028 covers the first) showed that
two of them could not run at all, and that a third class of state was not durable:

1. **The systemd path had no artifact store.** Its CI `publish.sh` required
   `ARTIFACT_BINARY_UPLOAD_URL` / `ARTIFACT_DOWNLOAD_URL` -- pre-signed URLs that nothing
   in netCI issues -- and the worker's verifier expected the binary *and* the cosign
   bundle to be files on its own disk, which they are not: the bundle was written in the
   Jenkins pod's workspace. The path could not have run anywhere.
2. **The Kubernetes path passed cluster nodes as Ansible hosts.** DCIM lists the kind
   nodes under the module; the Portal handed them to the worker as `target_hosts`, the
   worker added `--limit <nodes>` to a `hosts: localhost` play, and Ansible selected
   nothing. It also resolved `kubernetes.core.helm` through the invoking user's
   `~/.ansible`, where it was not installed.
3. **Maintenance mode and agent telemetry were process-local dicts.** Both decide whether
   a deployment may proceed (the DCIM pre-flight gate). An API restart forgot every
   server in maintenance; a second replica never saw what the first was told; the
   telemetry endpoint answered `normal 15/35/25%` for servers no agent had ever
   reported for; and the heartbeat wrote dataclasses into a dict the gate read as
   dictionaries, so the first real heartbeat would have crashed the gate.
4. **An unconfigured DCIM reported targets `healthy`.** The pre-flight helper's *pass*
   result is a truthy object; the unconfigured catalog returned it as its own answer.
5. **"Apply configuration" (redeploy without rebuild) had never run against PostgreSQL.**
   It acquired the lease before inserting the deployment row, which the lease's foreign
   key refuses; the in-memory store enforces no constraint, so every test passed. After
   a rollback it also chose the newest deployment record -- the rolled-back one, whose
   run is `rolled_back` -- and refused with NO_DEPLOYABLE_ARTIFACT although the
   environment was serving a verified digest. The UI called the result "Configuration
   Applied Successfully" with a lead time the API no longer returned.

## Decision

**One artifact store.** Every artifact netCI deploys is an OCI object in the registry,
identified by its manifest digest and signed with the same key:

- A container image is pushed as an image (unchanged).
- A Linux binary is pushed as a one-layer OCI artifact with `cosign upload blob`, then
  `cosign sign` / `cosign verify` exactly as an image. `artifactRef` is
  `host/repo@sha256:<manifest>`; the file's own sha256 is recorded beside it.
- The worker verifies the reference with `cosign verify` (no `verify-blob`, no bundle
  file), then `backend/app/adapters/oci_blob.py` fetches the single layer: the manifest
  bytes must hash to the verified digest, the blob must hash to the layer digest the
  manifest names, and the file is written under a content-addressed name. The playbook
  receives `artifact_path` and `artifact_sha256` (the layer digest, set by the worker
  from a manifest it verified -- never from a pipeline parameter).
- `imagePullHost` applies to binaries as it does to images: a locator, not an identity.
- Plain HTTP to a registry is refused unless the host is loopback or
  `NETCI_REGISTRY_ALLOW_HTTP` says the lab registry is plaintext.

**Kubernetes targets carry no Ansible hosts.** DCIM's cluster nodes are inventory facts
and are validated as such; `target_hosts` is empty for the kubernetes runtime and
`--limit` is only ever added for docker and systemd. The worker runs Ansible with
`NETCI_ANSIBLE_COLLECTIONS_PATH` and refuses to start outside local mode if a collection
named in `deploy/ansible/requirements.yml` cannot be resolved.

**Server state is stored, not remembered.** `backend/app/adapters/dcim.py` exposes a
`ServerStateStore` bound at startup to the platform database (`DatabaseServerState`);
maintenance mode and the latest telemetry per server (`server_telemetry`, migration
0020) are read and written through it by the API, the WebSocket heartbeat and the DCIM
catalogs. The in-memory implementation exists for tests and is the store there, not a
cache. `/api/v1/servers/{name}/telemetry` answers 404 `TELEMETRY_UNKNOWN` when no agent
has reported; it never invents numbers.

**An unconfigured DCIM never calls a target healthy**; only a refusal from the
pre-flight helper is returned as the catalog's answer.

**A redeploy starts from what is in service.** `source_run_in_service` takes the digest
the environment serves (healthy or rolled-back-to) and finds the run that built it; the
deployment row is written before its lease in the same transaction; a PostgreSQL-backed
test holds the ordering. The UI and OpenAPI say "deployment started / waiting for a
reviewer" and show the status the worker will move -- never "applied".

**Health gates compare identity.** The docker playbook requires the answering service
to report the deployed digest; the systemd playbook requires it to report the commit it
was built from (`VERSION` is the commit; the worker passes `commit_sha`). A stale
process on the port cannot pass for the new release.

## Consequences

- No MinIO/S3, no pre-signed URLs, no shared filesystem: the lab and a production
  deployment need the same one registry. The trade is that a binary is one more OCI
  repository and the worker needs network access to the registry (it already did, for
  cosign).
- `artifactDigest` for a binary is its manifest digest, not the file's sha256. Evidence
  carries both (`artifact-blob-sha256.txt`); the run record carries the identity.
- The edge-agent WebSocket registry (`_ACTIVE_RUNNERS`) remains process-local by nature
  (a socket belongs to one process). It holds no durable state; command dispatch to an
  agent connected to another replica is not supported and is documented as such.

## Rejected

- Pre-signed upload URLs to an object store: a second store with its own credentials,
  lifecycle and identity model, for one artifact kind.
- Downloading the binary on the *target* with `get_url`: the target would then need
  registry access and the worker could not verify what was installed.
- Keeping telemetry in memory "because it is transient": the gate reads it, so a replica
  that never received the heartbeat would deploy onto a host the other replica knows is
  full.
