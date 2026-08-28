# Security model

## Protected assets

Source, build credentials, signing identity, artifact bytes/digests, SBOM/scan reports, deployment credentials, approval identity and audit history are protected assets.

## Trust boundaries

- Browser/Backstage to netCI API.
- netCI to Jenkins/Temporal/artifact stores.
- Jenkins controller to ephemeral build pod.
- Host/control plane to Docker and Systemd VMs.
- Build/publish boundary to deployment policy.

## Identity, roles and separation of duties

Every governance claim netCI makes — production needs approval, each transition names an
actor, the audit trail is evidence — rests on the platform knowing who the caller is. An
actor read from a request body is a claim, not an identity: anyone can send
`{"actor": "the-cto"}`. So netCI takes the actor from the verified credential and ignores
any the caller supplies.

### Choosing an authentication mode

`NETCI_AUTH_MODE` selects the implementation, the same way `NETCI_CI_MODE` selects a CI
engine. The domain never imports the auth module; the composition root guarantees that the
actor string it passes came from a verified identity.

| Mode | Credential | Use it for |
|---|---|---|
| `none` | none; every caller is `anonymous` with every role | one developer, one laptop |
| `token` | bearer token, stored as a SHA-256 hash in `NETCI_AUTH_TOKENS_FILE` | a pilot, or machine-to-machine access where there is no IdP |
| `oidc` | bearer JWT from the corporate identity provider | **the production choice** |

`none` is not merely permissive — netCI in that mode **refuses any request that does not
arrive from loopback**. Forgetting to configure authentication on a host with an open port
therefore cannot publish production approval to the network; it produces a clear
`AUTH_NOT_CONFIGURED` refusal instead. `GET /healthz` reports the active mode under
`engines.auth`, so an operator can see the posture from outside.

### Roles

Roles are deliberately **not hierarchical in code**: a reviewer is not implicitly a
developer. Granting both is a decision someone makes explicitly, not one inherited by
accident.

| Role | May |
|---|---|
| `viewer` | read everything; change nothing |
| `developer` | create applications and systems, run dev/staging pipelines |
| `reviewer` | everything a developer can do to production: run production pipelines, approve and reject |
| `platform-admin` | as reviewer, plus platform administration; not team-scoped |
| `pipeline` | report build and deployment *results* only |

`pipeline` is a machine role, granted by `NETCI_PIPELINE_API_KEY` and never to a person.
It has no human escape hatch: a platform-admin cannot post a CI result, because asserting
what a build produced is the build's job and forging one would defeat the supply-chain
gate. Symmetrically the pipeline key cannot approve a deployment.

### Separation of duties

`POST /deployments/{id}/approve` compares the approver against `pipeline_runs.started_by`
— the verified subject that started the run — and refuses when they match, with
`SEPARATION_OF_DUTIES`. Without this the approval step degrades into a second button press
by the same person, which an audit will read as no control at all.

Two deliberate exceptions:

- With `NETCI_AUTH_MODE=none` every caller is the same subject, so the check would refuse
  every approval while separating nobody. It is inactive there, and `/healthz` says why.
- Runs created before authentication existed have no `started_by`. An unknown requester is
  treated as "cannot be shown to be the same person" and allowed, rather than locking out
  every deployment that predates the upgrade.

### Issuing and revoking tokens

The token file stores hashes, never tokens, so a leaked copy — or a backup of one — grants
nothing, and a lost token is replaced rather than recovered.

```bash
export NETCI_AUTH_TOKENS_FILE=/etc/netci/tokens.yaml
python scripts/netci_token.py issue --subject dana --name "Dana Developer" --role developer
python scripts/netci_token.py list
python scripts/netci_token.py revoke --subject dana
```

netCI re-reads the file when its mtime changes, so both take effect without a restart.

### Connecting a corporate identity provider

```bash
NETCI_AUTH_MODE=oidc
NETCI_OIDC_ISSUER=https://login.microsoftonline.com/<tenant>/v2.0
NETCI_OIDC_AUDIENCE=api://netci
NETCI_OIDC_ROLE_MAP=netci-admins=platform-admin,release-managers=reviewer,engineers=developer
```

Verification is strict on purpose, because every relaxation is a way in. The algorithm
comes from the key rather than the token header, so `alg: none` and the HMAC family are
refused outright — both are classic JWT forgeries. Issuer, audience, expiry and a matching
`kid` are all required. An unknown `kid` forces a JWKS refetch, so a key rotation does not
lock everyone out until a cache expires. A token whose groups map to no netCI role is
refused with `403`, not admitted with no rights.

`backend/tests/test_auth_oidc.py` signs tokens with a real key pair and asserts each of
those refusals, including the algorithm-confusion attack where a public key is submitted
as an HMAC secret.

### Ownership: which applications, not just what kind of action

Roles are global — they say what *kind* of thing someone may do. On their own, a
`developer` role lets anyone run any team's pipeline and a `reviewer` role lets anyone
approve any team's production release. That is workable for one team and wrong for an
organisation, where "who may deploy this" is a property of the application.

An application carries an `ownerTeam`, and a principal carries the teams it belongs to
(`teams:` in the token file, or the IdP group claim named by `NETCI_OIDC_TEAMS_CLAIM`,
default `groups`). The rule is checked wherever someone *acts* on an application: starting
a pipeline, approving its deployment, rolling one back. Reads are not team-scoped — a
delivery platform is more useful when everyone can see the state of the estate, and
visibility carries far less risk than action.

Two deliberate choices:

- **A platform-admin is not team-scoped**, and does not have to join every team in the
  organisation to administer the platform.
- **An unowned application is unrestricted**, so adopting ownership does not break
  applications created before it existed. Once every application has an owner, set
  `NETCI_REQUIRE_APPLICATION_OWNER=true`: an unowned application then becomes
  platform-admin only, and new ones must name a team. The door is open only for as long as
  the migration needs it.

You may only hand an application to a team you belong to. Otherwise `ownerTeam` would be a
field anyone could set to anything, which is a label rather than a control.

### What this does not do

netCI has no session cookies, no password store and no user database: it verifies a
credential per request and holds no long-lived secret of its own beyond the token hashes.
Rate limiting and tenant isolation are **not** implemented. Ownership is a single flat
team per application — there is no hierarchy, no per-environment delegation ("team A may
deploy to staging but not production"), and no group nesting beyond whatever the identity
provider flattens into the claim.

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

An artifact that needs to ship despite a fixable finding needs a recorded, time-boxed
exception with an owner. See **Exceptions** below.

## Exceptions

A gate with no legitimate way through does not stay switched on. Sooner or later a team
needs to ship with a known finding — an upstream fix that is not released yet, a false
positive, a vulnerability in a code path the service never reaches — and if the only option
is to disable the gate, that is what will happen, for everyone, permanently. So there is a
way through, and it is deliberately narrow.

`NETCI_SECURITY_EXCEPTIONS_FILE` points at a register (see `security-exceptions.example.yaml`).
Each entry names:

| Field | Why it is mandatory |
|---|---|
| `cve` | a waiver covers one finding; nobody can take responsibility for "three highs" |
| `artifactDigest` | an immutable digest, never a tag — a tag waiver would follow that tag onto a different image |
| `owner` | who is accountable for closing it |
| `approvedBy` | who accepted the risk; should not be the owner |
| `expires` | a date, after which the finding blocks again on its own |
| `reason` | what an auditor, or you in six months, needs in order to judge it |

Keep the register in Git. The review on the pull request that adds an entry is the control;
the history is the record of who accepted which risk and when.

**What an exception does not do.** It applies to the vulnerability gate and to nothing
else: a waived CVE never lets through a missing SBOM, an unverified signature, a mutable
tag, or an artifact whose evidence describes a different digest. A `deny` already recorded
against the evidence still wins. And a scan that reports only counts, with no identifiers,
cannot be waived at all — there would be nothing to name.

**A waiver applies from the next publish, not retroactively.** A `deny` already recorded
against an artifact's evidence is binding, so adding an entry does not release an artifact
that was already refused — the pipeline is re-run, CI publishes evidence again, and the
waiver applies at that point. This costs a build and is deliberate: it keeps the recorded
decision the one that was actually made, rather than something that can be changed
afterwards by editing a file.

**The expiry is the mechanism, not a formality.** An exception granted under pressure
cannot quietly become permanent; renewing it is another pull request. If entries accumulate
faster than they retire, the answer is a patched golden base image, not a longer register.

An allowed-with-waiver artifact is not recorded as `pass`. The decision reads `waived`, and
the reason carries the CVE, the expiry, the owner and the approver into the audit trail and
the deployment record, so a release that shipped with known findings is visible afterwards
rather than indistinguishable from a clean one.

`backend/tests/test_security_exceptions.py` asserts each of these limits, including the
case that an early version of the implementation got wrong: a waived CVE must not
short-circuit the signature check.

## How the gate is enforced

The policy is evaluated by one function, `evaluate_artifact_evidence`, and applied at two points:

1. when CI publishes evidence (`POST /pipeline-runs/{id}/security-evidence`), which returns the verdict so a build can fail immediately; and
2. when netCI is asked to create a deployment (`POST /pipeline-runs/{id}/ci-result`), which re-evaluates rather than trusting the earlier verdict.

The Temporal `validate_artifact` activity evaluates the same rules a third time before touching a runtime, because a workflow may run long after CI finished. A verdict already recorded as `deny` is binding: re-evaluation can never turn it into an allow.

All three points read the *same evidence*, so all three share its one weakness: `signature.verified` is a claim the build made about itself. Re-checking it cryptographically is a separate control — see [Re-verifying the signature at deploy time](#re-verifying-the-signature-at-deploy-time).

With `NETCI_REQUIRE_SECURITY_EVIDENCE=true`, an artifact with no published evidence at all is refused as well. The local reference default is `false` so the platform is usable without a full supply-chain stack; the acceptance profile turns it on.

## Re-verifying the signature at deploy time

The supply-chain policy checks `signature.verified`. That is a boolean CI wrote about its
own work: it records that the build *believed* the artifact was signed. If the CI system is
compromised, a build script is edited, or evidence is replayed from another pipeline, the
boolean still reads `true`.

`NETCI_SIGNATURE_VERIFY_MODE=cosign` closes that gap. Before any runtime is touched, the
Temporal worker re-runs cosign against the digest it is about to deploy, with a public key
netCI holds — minutes or hours after the build, on a different host, under a different
trust root.

```bash
NETCI_SIGNATURE_VERIFY_MODE=cosign
NETCI_COSIGN_PUBLIC_KEY_FILE=/etc/netci/cosign.pub
```

A **public** key, deliberately. netCI never needs the signing key, and holding one would
let the deployment host mint the signatures it is supposed to be checking.

It fails closed on every path that is not a clean verification: a missing cosign binary, a
missing key, an artifact with no recorded reference, an unreachable blob, a non-zero exit,
or a timeout. The target is always `repository@sha256:…` — never the tag CI recorded, which
would prove something about whatever that tag points at now rather than about the artifact
being deployed.

`NETCI_SIGNATURE_REQUIRE_TLOG` must match how the artifact was signed. The checked-in CI
scripts sign with `--tlog-upload=false`, which is right for a local or air-gapped registry,
and cosign then refuses to verify unless told the same — it reports "signature not found in
transparency log", which reads like a bad signature but is a configuration mismatch. With a
real Rekor deployment set it `true`, and a signature that was never logged is then
correctly refused.

The default is `none`: verification needs a key and a reachable artifact, and a platform
that fails every deployment on a missing key is not safer, only broken. `/healthz` reports
which mode is configured, so "are we actually re-verifying?" is not a question anyone has
to answer by reading a worker's environment.

`backend/tests/test_signature_verification.py` runs the real cosign binary against a real
key pair: a genuine signature passes, tampered bytes are refused, a different key is
refused, and demanding an absent transparency-log entry is refused.

## Isolation and credentials

- Controller executors remain disabled; each build gets a dedicated pod/workspace.
- Cleanup runs after success, failure and cancellation; caches are project-scoped and separate from workspaces.
- Do not mount a host Docker socket in the production path.
- Secrets are references/environment injection, never plaintext in Git, JCasC, logs or evidence.
- Jenkins and runtime-adapter callbacks authenticate with a bearer API key; unauthenticated callers cannot change pipeline or deployment state.
- Local default credentials are development-only and must be replaced before any shared environment.

## Evidence required

The security release gate must include a real denied artifact plus raw SBOM, scanner JSON, signature verification output, policy decision, artifact digest, timestamps and correlation ID. Static booleans or screenshots alone are insufficient.
