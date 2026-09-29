# ADR-058: Shared pipelines -- one script, versioned in netCI, approved by a second person

Status: Accepted; **amended 2026-09-29** (see Amendment at the end: no approval step, only
build and publish required). Supersedes the per-module designer of ADR-057 (pipelines edited per
module and merged in the module's repository).

## Context

ADR-057 made a pipeline a property of one module: the designer opened a merge request in that
module's repository, one file per custom stage. What people asked for is different:

- A **Pipelines** area where the platform's pipelines are listed and new ones are created.
  A pipeline is **CI only** -- test, build, SBOM, scan, sign, publish. Deployment (CD) is
  netCI's Temporal worker, per module and per runtime; a pipeline has no system and no runtime.
- Creating one: click a stage on the left, its code is **appended to one script** on the
  right, edited there. Not one file per stage.
- A new module picks its pipeline **by name**.
- Stored in netCI, **versioned, and approved by a second person** (chosen over a GitLab
  repository with merge requests).

## Decision

1. *(Amended: see the end.)* **A shared pipeline** is a name, a description and versions. A version is one script; at
   most one version is `active`. Saving creates a `proposed` version; a *different*
   platform administrator approves it (it becomes `active`, the previous active one
   `superseded`) or rejects it. Same separation of duties as custom stages (ADR-030) and
   production approvals. Every step is audited.
2. **The script is one text, cut into stages by marker lines** at run time:

   ```bash
   # @stage unit-test "Unit Test" builtin
   netci-builtin unit-test
   # @stage lint "Lint Dockerfile"
   hadolint Dockerfile
   ```

   A `builtin` block names a stage netCI implements (unit-test, build, sbom,
   vulnerability-scan, sign, publish) and its body must be exactly `netci-builtin <id>`:
   netCI refuses (422) a builtin block with other content rather than running it. Any
   other block is the author's bash.
3. **Why the cut: credentials stay scoped.** Built-in blocks run the library's own code with
   only the credentials that stage needs -- the cosign key exists only inside Sign, registry
   credentials only in registry stages. Author blocks run in the builder container with
   **no** credentials, like custom stages today. A single `bash script.sh` would hand the
   signing key to whatever code the test step runs, including an unreviewed pull request's.
4. *(Amended: see the end.)* **Required stages are required.** build, sbom, vulnerability-scan, sign and publish must
   be present, in that order; checkout is implicit and always first; deploy and health-check
   are CD and refused in a pipeline. The signature and SBOM gates still decide deployability
   from the artifact itself (ADR-013/044/045), whatever the script says.
5. **A module refers to a pipeline by name** (`applications.shared_pipeline`). Each run
   resolves the pipeline's active version *when it starts* and records name, version and the
   script's sha256 on the run; a version approved later affects later runs only. A module
   whose pipeline has no active version cannot start a run (409), rather than falling back.
6. The library receives the author blocks as `NETCI_CUSTOM_STAGES` entries carrying their code
   (base64) and anchored after the preceding built-in, and the built-in ids as `NETCI_STAGES`.
   Nothing in the build decides which blocks exist.

## Consequences

- A pipeline edit reaches every module that uses it, after one approval. That is the point of
  sharing one, and why the approval is required.
- ADR-057's per-module designer is removed from the portal; its merge-request path is no longer
  the way pipelines change. Its API stays for now, so a merge already open still converges.
- The library runs an author block from its code: written beside the workspace, decoded by the
  shell, run in the builder with no credential bound (`netciRunCustomStages`). Contract tests
  hold the runner to binding nothing and to running exactly the approved bytes.
- A run's pin is checked by hashing the stored script at launch, not by reading the stored hash:
  an edit to the row changes the script and not the hash beside it (a test caught the first
  version comparing the two stored values). A pin that no longer matches fails the run, audited,
  instead of leaving it queued.
- Not verified until a module on the corp lab runs a shared pipeline with an author block.

## Amendment (2026-09-29): no approval step; only build and publish required

Decided by the project owner after using the designer (commits `3a7b899`, `695d08e`).

1. **Creating a pipeline or saving a new version takes effect immediately.** `POST /pipelines`
   and `POST /pipelines/{name}/versions` are open to anyone who may start pipelines
   (`developer` and above, not only `platform-admin`), and the new version is `active` at once;
   the previous active version becomes `superseded`. The reason: a developer creating a module
   must be able to create and pick a pipeline in one sitting, without waiting for a second
   administrator.
   The approve/reject endpoints and the `proposed` state remain in the API and the schema, but the
   portal's own flows no longer create proposed versions.
2. **Required built-ins are `build` and `publish`** (`REQUIRED_BUILTINS`). unit-test, sbom,
   vulnerability-scan and sign may be left out of a pipeline; the order of those present is still
   fixed, and deploy/health-check are still refused.

What still holds, and is what bounds the risk:

- **Every run pins the version it was queued with** (name, number, sha256), and launch re-hashes
  the stored script; a mismatch fails the run. A later edit changes later runs only.
- **Every create and every new version is audited** (`pipeline.created`,
  `pipeline.version_proposed`, with author and sha256), and the version history keeps every
  script.
- **Author blocks run without credentials**; the cosign key and registry credentials exist only
  inside the library's own stages.
- **Deployability is decided at deploy time, not by the pipeline**: an artifact without a valid
  signature, provenance or SBOM evidence is refused by the policy gate (ADR-008/044/045),
  whatever its pipeline contained. A pipeline without `sign`/`sbom` therefore builds artifacts
  that cannot be deployed where those gates are on.

Consequence accepted with this amendment: an edit to a shared pipeline reaches every module
using it **from its next run, without review**. Whoever can start pipelines can change what
every module's build runs (in the builder, without credentials). If that becomes unacceptable,
restoring review needs no schema change: `requires_approval` is still a parameter of the service.

