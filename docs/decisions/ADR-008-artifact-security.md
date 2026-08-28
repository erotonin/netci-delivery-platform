# ADR-008: Artifact security

Status: Accepted.

## Context

An artifact tag or caller-supplied boolean does not prove provenance or vulnerability policy compliance.

## Decision

Deployments accept only immutable digests. Syft SBOM, Trivy report and Cosign signature/verification evidence are stored and verified by a deny-by-default policy before deploy. Binary and image artifacts follow the same invariant.

Local Registry + MinIO + Syft + Trivy + Cosign is the accepted reference toolchain for the local handover. Portal and domain contracts expose normalized artifact, SBOM, vulnerability and signature results rather than vendor names. An internal registry, secret manager, scanner or signing service replaces the corresponding adapter/configuration without changing the delivery policy.

`signature.verified` in the evidence is a **record**, not a verification: it says the build
believed the artifact was signed. Where that distinction matters, netCI re-runs cosign
itself at deploy time against a public key it holds — a separate seam,
`NETCI_SIGNATURE_VERIFY_MODE`, documented in the [security model](../security-model.md).

Exceptions are time-boxed and named. A waiver covers one CVE on one artifact digest until
one date, with an owner and an approver, and applies to the vulnerability gate only — never
to the SBOM, the signature, or the digest requirement. A gate with no legitimate way through
gets switched off wholesale, so the way through is narrow rather than absent.

## Consequences

Registry/MinIO availability and signing identity management become local release dependencies. Exact Viettel vendor parity is not a local acceptance requirement. Exceptions require an explicit audited policy record; missing evidence is denial.

Deploy-time re-verification defaults to off, because it needs a public key and a reachable
artifact, and a platform that fails every deployment on a missing key is not safer. That
default is visible in `/healthz` rather than implied, so nobody has to assume which of the
two controls a given deployment relied on.

An artifact allowed under a waiver is recorded as `waived`, not `pass`, and the reason
carries the CVE, expiry, owner and approver into the audit trail — a release that shipped
with known findings stays visible afterwards.

## Verification

SEC-01 includes one allowed artifact and one real denial with raw evidence and correlation IDs.
`backend/tests/test_signature_verification.py` exercises the deploy-time verifier against
the real cosign binary and a real key pair: genuine signature accepted, tampered bytes
refused, wrong key refused. `backend/tests/test_security_exceptions.py` and
`test_ci_evidence_contract.py` cover the exception register and the shape CI publishes.
