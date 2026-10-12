# Re-verify the exact artifact at deployment time

A CI result saying `signatureVerified: true` is useful evidence, but it is not a
deployment control. The producer of that statement may be compromised, the
statement may describe a different build, or a mutable tag may have moved.

The deployment system should independently verify the immutable artifact it is
about to execute, using a trust root controlled outside the build job.

## OCI artifact

Record both the repository and digest in CI, then remove any mutable tag and
verify the digest-qualified reference:

```bash
cosign verify --key cosign.pub registry.example/app@sha256:<64-hex-digest>
```

The deployment manifest must use that same `repository@digest`. After rollout,
read the workload back from the runtime and compare its image field with the
approved value.

## Blob artifact

Record the blob digest and signature bundle. Make the blob and bundle available
to the isolated deployment worker, verify them, and independently hash the blob
before installation:

```bash
cosign verify-blob --key cosign.pub --bundle artifact.bundle artifact.bin
sha256sum artifact.bin
```

## Failure behaviour

Verification should fail closed when the binary is missing, the artifact cannot
be reached, the digest is malformed, the bundle is absent, Cosign exits non-zero
or verification exceeds a bounded timeout. Keep signing keys in CI and public
verification material in the deployment system; the deployer should not be able
to mint the signature it is responsible for checking.

Transparency-log policy must be explicit. Public Sigstore flows normally verify
Rekor inclusion. An intentionally air-gapped, key-based workflow may disable
that requirement, but the decision should be configuration visible in health
and audit records, not an implicit fallback after verification fails.
