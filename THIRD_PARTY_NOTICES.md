# Third-party open-source software

netCI integrates the projects below. Each project remains licensed by its
authors under its own terms; the netCI Apache-2.0 license does not replace those
terms. Exact resolved versions are authoritative in `frontend/package-lock.json`,
`backend/requirements*.txt`, `deploy/ansible/requirements*`, container files and
`docker-compose.yml`.

This inventory covers direct dependencies and major runtime tools. Transitive
JavaScript packages and their SPDX identifiers are recorded in the npm lockfile.
A release SBOM remains the authoritative machine-readable inventory.

| Project | Role | License family |
|---|---|---|
| FastAPI | HTTP application framework | MIT |
| Uvicorn | ASGI server | BSD-3-Clause |
| Pydantic | validation and schemas | MIT |
| HTTPX | HTTP client | BSD-3-Clause |
| Temporal Python SDK / Temporal | durable workflow client and server | MIT |
| Psycopg | PostgreSQL adapter | LGPL-3.0 |
| PyYAML | YAML parsing | MIT |
| cryptography | OIDC signature primitives | Apache-2.0 OR BSD-3-Clause |
| React / React DOM | Portal UI | MIT |
| Lucide | Portal icons | ISC |
| Inter via Fontsource | Portal font | OFL-1.1 |
| Vite / Vitest | frontend build and tests | MIT |
| TypeScript | frontend language tooling | Apache-2.0 |
| Playwright | browser acceptance tests | Apache-2.0 |
| axe-core | accessibility tests | MPL-2.0 |
| PostgreSQL | durable database | PostgreSQL License |
| Jenkins | CI controller and agents | MIT |
| Nginx | Portal gateway | BSD-2-Clause |
| Distribution Registry | OCI registry | Apache-2.0 |
| MinIO | S3-compatible evidence storage | AGPL-3.0 |
| Ansible Core | deployment automation | GPL-3.0-or-later |
| community.docker | Docker Ansible collection | GPL-3.0-or-later |
| kubernetes.core | Kubernetes Ansible collection | GPL-3.0-or-later |
| Kubernetes / Kind / Helm | orchestration and local cluster | Apache-2.0 |
| Buildah | rootless container build | Apache-2.0 |
| Syft | SBOM generation | Apache-2.0 |
| Trivy | vulnerability scanning | Apache-2.0 |
| Cosign / Sigstore | artifact signing and verification | Apache-2.0 |
| Backstage | developer portal integration | Apache-2.0 |
| Roadie HTTP Scaffolder module | Backstage HTTP action | Apache-2.0 |
| GitHub-maintained Actions | source checkout, language setup and artifact upload | MIT |
| Anchore SBOM Action | release SBOM generation through Syft | Apache-2.0 |

Before distributing a release, regenerate its SBOM and retain all license and
notice files shipped by container images and package managers. This document is
an engineering inventory, not legal advice.
