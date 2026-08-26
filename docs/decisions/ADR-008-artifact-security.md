# ADR-008: Artifact security

Status: Accepted.

## Context

An artifact tag or caller-supplied boolean does not prove provenance or vulnerability policy compliance.

## Decision

Deployments accept only immutable digests. Syft SBOM, Trivy report and Cosign signature/verification evidence are stored and verified by a deny-by-default policy before deploy. Binary and image artifacts follow the same invariant.

Local Registry + MinIO + Syft + Trivy + Cosign is the accepted reference toolchain for the local handover. Portal and domain contracts expose normalized artifact, SBOM, vulnerability and signature results rather than vendor names. An internal registry, secret manager, scanner or signing service replaces the corresponding adapter/configuration without changing the delivery policy.

## Consequences

Registry/MinIO availability and signing identity management become local release dependencies. Exact Viettel vendor parity is not a local acceptance requirement. Exceptions require an explicit audited policy record; missing evidence is denial.

## Verification

SEC-01 includes one allowed artifact and one real denial with raw evidence and correlation IDs.
