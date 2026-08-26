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

## Isolation and credentials

- Controller executors remain disabled; each build gets a dedicated pod/workspace.
- Cleanup runs after success, failure and cancellation; caches are project-scoped and separate from workspaces.
- Do not mount a host Docker socket in the production path.
- Secrets are references/environment injection, never plaintext in Git, JCasC, logs or evidence.
- Jenkins and runtime-adapter callbacks authenticate with a bearer API key; unauthenticated callers cannot change pipeline or deployment state.
- Local default credentials are development-only and must be replaced before any shared environment.

## Evidence required

The security release gate must include a real denied artifact plus raw SBOM, scanner JSON, signature verification output, policy decision, artifact digest, timestamps and correlation ID. Static booleans or screenshots alone are insufficient.
