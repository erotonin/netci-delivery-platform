# ADR-044: SLSA provenance is checked where the artifact runs

Status: Accepted.

## Context

netCI signs every image with cosign, and the worker re-verifies the signature before it
deploys (ADR-008). A signature proves that the holder of the key signed *something*. It
does not say what was built. An image built from any commit, or any repository, and then
signed with the key passes both checks. The only record linking a digest to a commit was
netCI's own `pipeline_runs` row. A cluster admission check, or a second netCI, cannot
consult that row, and a compromised build could report whatever digest it liked for it.

## Decision

1. **The Sign stage attests SLSA v1 provenance** with the same key (`cosign attest
   --type slsaprovenance1`), then verifies it on the spot (`cosign verify-attestation`).
   The predicate is built by `netci_callback.py provenance`, only from what netCI told
   the build to do and from Jenkins itself: the repository, the exact commit, the app
   directory, the image name, the run id, the controller and the build URL. Credentials
   in the repository URL are stripped, because the attestation lives in the registry
   where anyone who can pull can read it.
2. **The API binds provenance to the run when evidence is published.** A commit or a
   repository other than the one netCI dispatched is a `deny`. The verdict is stored with
   the evidence, and a stored deny binds every later evaluation (`evaluate_artifact_evidence`),
   so a mismatch cannot be re-read as allow later on. Provenance that is present but
   unverified is refused even when provenance is not required.
3. **The worker checks it again, from the registry, just before it deploys.** It runs
   `cosign verify-attestation` with netCI's key against the pinned digest. It then
   requires a statement for that digest that names the deployment's commit, in both
   `externalParameters` and `resolvedDependencies`, and the module's repository.
   Both the commit and the repository come from the server, not from evidence CI wrote.
   `NETCI_SIGNATURE_VERIFY_MODE=none` cannot verify provenance, so requiring provenance
   in that mode fails closed rather than passing silently.
4. **`NETCI_REQUIRE_PROVENANCE`** (chart: `supplyChain.requireProvenance`, default
   `true`) refuses a container image that has no verified provenance. A systemd binary is
   signed as a blob and has no attestation. It is recorded as `not_applicable`, never as a
   pass. The code default is `false`, so the systemd lab stack, which still runs the old
   library, keeps working.

## Consequences

- This is SLSA Build **L2**, not L3. The provenance is signed and produced by the hosted
  build, but it is generated on the same agent that ran the build's code, with the key
  bound in that step. A build that steals the key can forge provenance too. What this
  closes is a different gap: an image signed with a legitimate key but built from some
  other source. Reaching L3 needs a signer the build cannot reach, such as keyless
  signing from a separate identity, or attestation by the controller rather than the agent.
- A controller on an older library produces images with no provenance. With the chart
  default, those images are refused. Upgrade the library before the chart.
- `DeliveryInput` gains `source_repository`. It has a default, so a workflow that started
  before this change resumes, and its provenance check then fails closed for want of a
  repository only if provenance is required.
- A Kubernetes admission check could verify the same attestation without netCI's
  database. That is not built yet (`admission.py` still checks netCI's evidence).
