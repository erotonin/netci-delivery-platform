# ADR-059: netCI decides the Jenkins controller and every plugin

Status: Accepted. Extends ADR-056 (which covered the build tools) to the controller itself.
Not verified live yet: see Consequences.

## Context

ADR-056 made netCI the authority for the tools a build runs (syft, trivy, cosign, buildah).
The controller that runs those builds was left out:

- `jenkins/plugins.txt` pinned the 13 plugins netCI uses and nothing else.
  `jenkins-plugin-cli` resolved their ~70 dependencies to whatever the update site offered on
  the day the image was built, so two builds of the "same" controller image differed.
- Nobody compared what a controller runs with what was intended. A plugin added or upgraded
  by hand on the controller ran inside every build, unnoticed.
- Resolving the set on 2026-09-29 showed the pinned controller (2.541.1) carried known
  vulnerabilities: credentials-binding 719 (SECURITY-3672, -3790, path traversal),
  pipeline-groovy-lib 798 (SECURITY-3796, -3815) and workflow-multibranch 841
  (SECURITY-3729, exposure of System-scoped credentials). The last two have fixes only for
  core >= 2.555.x.

## Decision

1. **`toolchain/versions.yaml` declares the controller**: its base image pinned by digest,
   the image name and tag netCI builds, the plugins netCI's pipelines use (`requires`), and
   **every** plugin with its exact version, dependencies included (`plugins`).
   `jenkins/plugins.txt` and the Dockerfile's base are generated from it
   (`scripts/toolchain_sync.py`, `--check` in a contract test), the same arrangement as
   `backend/schema.sql` and the migrations.
2. **The image is the declaration or it is not built**: `Dockerfile.controller` compares the
   installed set with `plugins.txt` after `jenkins-plugin-cli` and fails on any difference.
3. **Before every build netCI reads the controller's active plugins** and compares them with the
   declaration: `version`, `missing` and `undeclared` are all drift. A drifted controller, or
   one whose list cannot be read, does not take the build; the router tries the next one and
   the run fails with `JENKINS_PLUGIN_DRIFT` / "plugin set unreadable" when none is left.
   `NETCI_TOOLCHAIN_ENFORCE=warn` (ADR-056's switch) logs and allows, for a rollout window.
4. **netCI's account reads, never administers**: `Overall/SystemRead` (enabled with
   `-Djenkins.security.SystemReadPermission=true`) is what the plugin list needs. Administer
   stays with people.
5. **`GET /toolchain` reports each controller's plugin drift**, read at request time, and the
   Toolchain page shows it next to the tool versions.
6. **The controller moves to 2.555.3 LTS** with the plugin set resolved against it, which clears
   the advisories above. Tag `2.555.3-netci1` (Harbor tags are immutable: a new set is a new tag).
7. **Rollout is `scripts/corp/up.sh`**: it builds and pushes the declared tag when Harbor does not
   have it, then upgrades the Helm release with `infra/corp/jenkins/values.yaml`, whose tag a
   contract test holds equal to the declaration.

## Alternatives rejected

- **Pin only the top-level plugins** (the old `plugins.txt`): reproducible in name only.
- **Let the build report plugins** from inside the pipeline: the controller would vouch for
  itself through code running in the build; a read by netCI's own account does not depend on
  what the build does.
- **Grant netCI Administer** to read the plugin list: far more than a read needs.

## Consequences

- A backend with this change refuses builds on every controller still running the old image or
  lacking SystemRead -- by design, fail closed. The live systemd stack (`jenkins-a/b`) and the
  `netci-local` install run older controllers: roll them out, or run them with
  `NETCI_TOOLCHAIN_ENFORCE=warn`, before deploying this backend there.
- Upgrading a plugin is a reviewed change to `versions.yaml`, then `toolchain_sync.py`, then a new
  controller tag.
- **Not yet verified live**: the corp cluster's control planes lost quorum after a host reboot
  (Docker reassigned their addresses; the etcd peer certificates name the old ones). Pinning them
  back (`scripts/corp/pin_node_ips.sh`) restarts the cluster and is an operator's action. Until a
  build runs on `2.555.3-netci1` with netCI checking its plugins, this ADR records a decision, not
  evidence.
