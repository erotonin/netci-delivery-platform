# ADR-054: Builds authenticate to git and the registry, and forks never push

Status: Accepted.

## Context

A pre-integration review for a company Jenkins found four gaps in the shared pipeline.

- **No credential was ever bound.** The GitSCM checkout had no `credentialsId`, the project
  cache's `git clone --mirror`/`fetch` ran anonymously, and buildah, syft, trivy and cosign
  had no registry auth. The lab's git server and registry are anonymous, so nothing noticed;
  a private repository or an authenticated registry fails the first build.
- **TLS and the transparency log were never forwarded.** `common.sh` defaults
  `REGISTRY_TLS_VERIFY=false`, `sign.sh` defaults `--allow-insecure-registry` and no tlog
  upload, and netCI sent neither. Every build skipped TLS verification whatever
  `registry.allowHttp` said, and with `supplyChain.signatureRequireTlog: true` (the chart's
  default) no signature a build made could verify.
- **The cosign key could not have a password.** The Sign stage hard-coded `COSIGN_PASSWORD=""`.
- **A fork's verify-only build pushed its image.** ADR-043 skips Sign and Publish when
  `NETCI_PUBLISH=false`, but `sbom.sh` and `scan.sh` called `push-image.sh` first. An
  unsigned image built from unreviewed outside code reached the registry. Nothing could
  deploy it, because netCI refuses a digest from such a run, but it was still there.

The review also found a leak. The signing key was written to `NETCI_OUTPUT_DIR` and removed
only after `sign.sh` succeeded. When Sign failed, the key stayed in the directory that
`post { always }` archives as build artifacts.

## Decision

1. **Credential ids are part of the job script, not build parameters.** Three optional
   ids — `gitCredentialsId`, `registryCredentialsId`, `cosignPasswordCredentialsId` —
   follow `cosignCredentialsId`: env `NETCI_*_CREDENTIALS_ID`, Helm `jenkins.*`, written by
   `_job_config_xml`, and reconciled before every build. A parameter could be set by anyone
   allowed to start the job by hand. The ids go into Groovy string literals, so an id
   Jenkins itself would refuse (anything but `[A-Za-z0-9_.-]`) stops netCI at startup. It
   is not escaped. Empty means what it always meant: anonymous git, anonymous registry, a
   key with no password. The lab's JCasC needs no new credential.
2. **Git** uses the git plugin's own mechanisms. GitSCM gets `credentialsId`. The mirror
   commands run inside `gitUsernamePassword`, which provides `GIT_ASKPASS` for them only,
   and with an empty `credential.helper`, so a helper on the agent image cannot store the
   password. The password is never in a URL, so it never reaches the mirror's config.
3. **Registry**: `netciRegistryAuth` binds a Username with password credential for one
   stage. `buildah login --password-stdin` writes one file under umask 077, beside the
   workspace (never in it). `REGISTRY_AUTH_FILE` and `DOCKER_CONFIG` both point at it,
   because containers-auth.json is the `auths` section of Docker's config.json. buildah
   reads the first; cosign, syft and trivy read the second through go-containerregistry's
   default keychain, and trivy also uses it for its DB mirror. The file is removed in a
   `finally`, and again in `post { always }`. Only Build, SBOM, Scan, Sign and Publish
   bind the credential. Only the push host is logged into: the base image and the Trivy
   mirror are in that registry, and no build step contacts the pull host.
4. **TLS and tlog are always sent.** `REGISTRY_TLS_VERIFY` is `false` only when
   `NETCI_REGISTRY_ALLOW_HTTP` is true. `COSIGN_TLOG_UPLOAD` is `true` exactly when
   `NETCI_SIGNATURE_REQUIRE_TLOG` is. Both are parsed the way the worker and the signature
   verifier parse them. cosign's `--allow-insecure-registry` now follows
   `REGISTRY_TLS_VERIFY`, and asking for it while TLS is verified is refused. Malformed
   values are refused, not read as true or false. The scripts keep their old defaults for
   a build that sends neither (older netCI, a build started by hand).
5. **A verify-only build never reaches the registry.** When `NETCI_PUBLISH=false`, sbom.sh
   reads `oci-archive:<archive>` and scan.sh runs `trivy image --input` on the OCI layout
   unpacked from that archive. Trivy cannot open the archive tar itself. The layout goes
   in `$TMPDIR`, not `WORKSPACE_TMP`: Jenkins names that directory `<job>@tmp`, and trivy
   read `dir@…` as a digest reference. `push-image.sh` refuses such a build whoever calls
   it, and the systemd template's `sign.sh`, its only upload, refuses one too. The
   pipeline gives such a build no registry credential, and `netciRegistryAuth` refuses if
   it is ever handed one. The builder container outlives each stage, so a daemon left
   behind by a fork's unit test could read a credential written later. Git credentials
   are still bound for such a build, because a fork's pull request ref in a private
   repository needs them. Checkout runs before any of the fork's code.
6. **The key password** is an optional Secret text. It is bound in Sign only and passed to
   sign.sh as `COSIGN_PASSWORD`. The key now lives in `WORKSPACE_TMP/netci-signing`, is
   removed by an `EXIT` trap, and `*.key` is excluded from archived artifacts. Scripts
   that handle a secret start with `set +x`, so the value is never traced at all instead
   of relying on Jenkins masking it in the trace.

## Consequences

- With `signatureRequireTlog: true`, the default, Sign now uploads to the transparency
  log, which is public Rekor unless cosign is configured otherwise. The agent needs a
  route to it. Air-gapped installs set `signatureRequireTlog: false`, as the lab does. A
  private Rekor URL is not configurable yet.
- With `registry.allowHttp: false`, the default, builds now verify registry TLS. The
  agent must trust the registry's CA; the toolbox image carries only the system bundle.
  The lab example sets `allowHttp: true`, and so do `infra/lab/real-local.env.template` and
  the live stack's env. `docker-compose.yml` now sets `NETCI_REGISTRY_ALLOW_HTTP=true` for
  its HTTP registry, and `scripts/jenkins_lab.sh` prints it.
- A verify-only build whose base image needs an authenticated pull fails at Build. That is
  intended; a separate read-only pull credential for forks would be a new decision.
- The mirror path supports HTTPS credentials only. An SSH URL with an SSH-key credential
  works through GitSCM, not through the cache's mirror.
- `scripts/jenkins_preflight.py` checks the optional credentials exist when they are named.
- **Verified outside Jenkins (2026-09-25).** The real CI scripts and the exact
  `buildah login` snippet from `netciRegistryAuth.groovy` were run with buildah 1.33.7,
  syft 1.46.0, trivy 0.71.2, cosign 2.4.1 and a `registry:3.1.1` that requires a
  password:
  - a verify-only SBOM and a real vulnerability scan came from the local archive, and the
    registry catalog stayed empty;
  - an anonymous push was refused ("authentication required");
  - after the login, the push, `syft registry:`, trivy by digest, and signing and
    verifying with a password-protected key under a `…@tmp` path all passed;
  - an empty password failed to decrypt the key;
  - the auth file was mode 600, and the password appeared in no log.

  All `vars/*.groovy` parse with the Groovy 2.4.21 in the lab's Jenkins image.
- **Not verified live:** none of this has run through a Jenkins. Not tested: the
  `gitUsernamePassword` and `usernamePassword` bindings in a real build, a private git
  server, a company registry with TLS, cosign 3.x (the toolbox's version) with a
  password, trivy 0.73 on an unpacked layout, and a Rekor upload.

## Addendum: the transparency log is named, never defaulted

Forwarding `COSIGN_TLOG_UPLOAD` from `supplyChain.signatureRequireTlog` (default true)
meant every company build would upload its signature to cosign's default log, the public
Rekor, publishing each internal image digest and the signing identity. So:

- `supplyChain.rekorUrl` (`NETCI_REKOR_URL`, https only) names the log. With
  `signatureRequireTlog: true` and no URL, the chart refuses to render and netCI refuses
  to start. The public Rekor is used only if its URL is written down.
- The build receives `COSIGN_REKOR_URL` and passes `--rekor-url` to sign, attest and
  their verification; `sign.sh` refuses to sign when told to upload with no URL. netCI's
  verifier checks against the same URL.
- A private Rekor's public key must also be trusted by cosign on the agents and in the
  netCI images. That is deployment configuration and is not automated or verified here.
