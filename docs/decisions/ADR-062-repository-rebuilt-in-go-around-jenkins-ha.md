# ADR-062: The repository is rebuilt in Go around Jenkins HA; the portal and CD are removed

Status: Accepted (2026-10-01).

## Context

ADR-060 and ADR-061 change what netCI is: a high-availability layer and an agent fabric for
the organisation's own Jenkins, which already deploys. Most of the existing code served the
old product -- a portal, a Temporal CD engine, deployment runtimes, a service catalog -- and
the backend is one Python process whose `main.py` alone is ~7,000 lines with those concerns
interleaved. Removing them piece by piece from that file would leave half-used paths behind.

## Decision

1. **New layout, Go for services, Java only inside Jenkins.**

   ```
   cmd/<service>/        one binary per service: intake, supervisor, fabric, readpath
   internal/             shared packages: jenkins client, store (PostgreSQL), lease/fencing,
                         queue, audit, config
   jenkins-plugin/       the Jenkins plugin (Java, Maven): fabric Cloud, dispatch bridge,
                         deploy guard step
   deploy/               Helm charts for the services and for a cell
   lab/                  the KVM + k3s + Longhorn lab and the chaos suite
   jenkins/              controller image, plugin set (generated from toolchain/versions.yaml)
   toolchain/            versions.yaml, the single declaration of tools and plugins
   docs/decisions/       every ADR, old ones kept as history
   ```

   Go because every piece is an infrastructure controller: Kubernetes client-go, Cluster API
   and the autoscalers this learns from are Go; one static binary per service.

2. **Removed** (the tag `netci-0.3-cd-portal` keeps them): the portal (`frontend/`), the Python
   backend (`backend/`) and its tests, Temporal workflows and runtime adapters, Ansible
   deploy playbooks and the Helm charts for netCI and the sample app (`deploy/`), sample apps
   and CI templates, the netCI shared library and the two-controller JCasC of the old lab,
   Backstage integration, the old OpenAPI document, the docker-compose stack, the kind-based
   corp lab cluster and its scripts, release and demo scripts, and their evidence files.

3. **Kept and carried forward:** `toolchain/versions.yaml` and its generator
   `scripts/toolchain_sync.py`; the controller image (`jenkins/Dockerfile.controller`,
   `jenkins/plugins.txt`); the corp lab's GitLab, Harbor, SeaweedFS and Velero (reused as the
   lab's SCM, registry, object store and DR); `docs/research/`; all ADRs.

4. **Reused ideas are rewritten, not copied:** the Jenkins REST client and router, admission
   budget and supersession, drift gates, leases and fencing tokens, the reconciler, webhook
   verification and de-duplication. Their Python versions stay readable at the tag.

5. **The quality bar of the old repository stays:** migrations are the schema source; every
   important transition is atomic, idempotent, audited and safe under concurrency; nothing
   is called live without a run against real infrastructure; secrets are never logged.

## Consequences

- The previous product cannot be built from this branch; it lives at the tag.
- Guides written for it move to `docs/archive/` and say so.
