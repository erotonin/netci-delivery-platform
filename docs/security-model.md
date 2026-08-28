# Security model

## Protected assets

Source, build credentials, signing identity, artifact bytes/digests, SBOM/scan reports, deployment credentials, approval identity and audit history are protected assets.

## Trust boundaries

- Browser/Backstage to netCI API.
- netCI to Jenkins/Temporal/artifact stores.
- Jenkins controller to ephemeral build pod.
- Host/control plane to Docker and Systemd VMs.
- Build/publish boundary to deployment policy.

## Supply-chain gate

For each artifact, CI must:

1. build once and publish by immutable digest;
2. generate an SBOM with Syft;
3. scan with Trivy under a versioned severity/exception policy;
4. sign with Cosign and record identity/key reference;
5. store evidence in Registry/MinIO;
6. have netCI verify evidence before any deployment.

A missing/invalid digest, SBOM, scan decision or signature is deny-by-default. Staging and production receive the same digest. Systemd binaries require equivalent blob digest/SBOM/scan/sign evidence, not an exemption from the gate.

## What the vulnerability gate blocks, and why

netCI blocks HIGH and CRITICAL findings **that have a published fix** (`trivy --ignore-unfixed`, overridable with `TRIVY_IGNORE_UNFIXED=false`).

This is a deliberate choice, not a relaxation. A finding with no upstream fix cannot be resolved by rebuilding, so failing every build on it would make the gate permanently red and therefore ignored — the failure mode a security gate exists to avoid. Findings without a fix are still counted and stored in the evidence record, so they remain visible and can be reported on; they just do not block a release that has no action available to it.

The corollary is that base images must be patched rather than merely pinned. The sample applications run `apk upgrade` on top of a pinned base for exactly this reason: pinning alone freezes a base at the vulnerabilities it shipped with, and the gate will (correctly) refuse it once fixes are published. When a build fails this gate, the fix is to rebuild on current packages — which is what the gate is asking for.

An artifact that needs to ship despite a fixable finding needs a recorded, time-boxed exception with an owner. That mechanism is not implemented; until it is, such a release is blocked.

## How the gate is enforced

The policy is evaluated by one function, `evaluate_artifact_evidence`, and applied at two points:

1. when CI publishes evidence (`POST /pipeline-runs/{id}/security-evidence`), which returns the verdict so a build can fail immediately; and
2. when netCI is asked to create a deployment (`POST /pipeline-runs/{id}/ci-result`), which re-evaluates rather than trusting the earlier verdict.

The Temporal `validate_artifact` activity evaluates the same rules a third time before touching a runtime, because a workflow may run long after CI finished. A verdict already recorded as `deny` is binding: re-evaluation can never turn it into an allow.

With `NETCI_REQUIRE_SECURITY_EVIDENCE=true`, an artifact with no published evidence at all is refused as well. The local reference default is `false` so the platform is usable without a full supply-chain stack; the acceptance profile turns it on.

## Isolation and credentials

- Controller executors remain disabled; each build gets a dedicated pod/workspace.
- Cleanup runs after success, failure and cancellation; caches are project-scoped and separate from workspaces.
- Do not mount a host Docker socket in the production path.
- Secrets are references/environment injection, never plaintext in Git, JCasC, logs or evidence.
- Jenkins and runtime-adapter callbacks authenticate with a bearer API key; unauthenticated callers cannot change pipeline or deployment state.
- Local default credentials are development-only and must be replaced before any shared environment.

## Evidence required

The security release gate must include a real denied artifact plus raw SBOM, scanner JSON, signature verification output, policy decision, artifact digest, timestamps and correlation ID. Static booleans or screenshots alone are insufficient.
