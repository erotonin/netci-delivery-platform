# Open-source map and release policy

netCI is licensed under Apache-2.0. External projects keep their own licenses;
see `THIRD_PARTY_NOTICES.md` and the release SBOM.

## What belongs in this repository

The control plane, Portal, contracts, integration adapters, reference delivery
templates and reproducible lab belong together because their acceptance gates
prove one end-to-end system. Reusable pieces should expose a small interface and
must not import netCI state merely to avoid duplication.

Current extraction candidates are:

| Candidate | Intended home | Status |
|---|---|---|
| Jenkins immutable-artifact Shared Library example | upstream patch for `jenkinsci/pipeline-examples` | prepared under `contrib/upstream` |
| Deploy-time Cosign verification guidance | upstream patch for Sigstore documentation | prepared under `contrib/upstream` |
| Immutable Docker/Kubernetes/Systemd deployment | `netci.delivery` Ansible Galaxy collection | collection metadata present; live integration gates remain authoritative |
| Artifact evidence evaluator | future standalone Python package | keep internal until its evidence schema is versioned independently |
| Backstage integration | future Backstage plugin | template exists; plugin extraction needs a stable authentication contract |

`contrib/upstream/manifest.yaml` is the machine-readable handoff: `prepared`
means the patch material exists locally, not that an upstream maintainer has
reviewed or accepted it.

## Public release checklist

1. Confirm the copyright holder shown in `NOTICE`.
2. Run `make oss-check` and `make release-portable`.
3. Run every affected live gate on its required host.
4. Confirm the pinned `signed-release` workflow attached the tag-derived source
   archive, SPDX SBOM, checksums and keyless Cosign bundles; build and attach an
   SBOM and signature for each separately released container.
5. Scan released images with Trivy.
6. Sign release artifacts and provenance with Cosign.
7. Publish checksums, changelog, migration notes and current evidence.
8. Create the Git tag only from the commit that produced the evidence.

The repo does not claim that historical files under `evidence/` certify a newer
commit. GitHub branch protection, private vulnerability reporting and OpenSSF
Scorecard must be enabled by an administrator after publication; repository
files cannot enable those account-level controls. The checked-in `dco` workflow
already enforces author-matching sign-offs and should be a required status check.
