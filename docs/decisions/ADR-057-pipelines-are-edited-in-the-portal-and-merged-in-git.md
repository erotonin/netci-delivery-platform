# ADR-057: Pipelines are edited in the portal and merged in git

Status: Accepted (implemented; not yet run against a real GitLab -- see Consequences).

## Context

A module's pipeline is an ordered list of catalog stages (ADR-042) set through the API.
People want what Azure DevOps offers: pick stages from a list, see and edit the code of
each, append their own. The stage catalog refused commands from the portal on purpose:
code that runs in a build must be reviewed where the code lives, in git.

## Decision

1. **A Pipelines page** lists every module's pipeline. **The designer** has the stage
   catalog on the left; clicking a stage appends it to the pipeline (in the order the
   template allows) and opens its code on the right. Built-in stages show their script
   read-only (from the shared library's resources). A *custom script* stage is edited
   there.
2. **Saving never runs anything.** It opens a merge request in the module's GitLab
   repository on a branch `netci/pipeline-<id>`: `.netci/pipeline.yaml` (the ordered
   stage ids, with `after` anchors for custom ones) and `.netci/stages/<stage-id>.sh` for
   each custom script. The server decides the repository (the module's SCM integration),
   the branch and the paths; the browser sends only stage ids and script text.
3. **The merge is the approval.** When GitLab reports the MR merged (the SCM webhook),
   netCI writes the stages as a configuration revision of the module, which follows the
   existing approval rules (production-touching changes need a second person). A custom
   script runs from the repository path at the commit being built, like any other repo
   code; no script text is stored in or executed from netCI.
4. **Module-level custom stages**: `pipelineConfig.customStages` holds `{id, name, after,
   script}` with `script` fixed to `.netci/stages/<id>.sh`; required catalog stages cannot
   be removed or reordered.

## Consequences

- GitLab only at first (the company's SCM); GitHub is refused with 422 until implemented.
- netCI needs a GitLab token that can push branches and open MRs on module repositories.
- **The merge is a trigger; the default branch is the state.** On a merge netCI reads
  `.netci/pipeline.yaml` at the default branch's *head* -- not the webhook body, and not
  that merge's own commit -- and reads the head again after writing, applying again if it
  moved. Two merges close together are delivered and processed in any order; reading each
  merge's own commit let the older one land last (found by the cross-model review). A
  delivery that loses every retry to a concurrent writer answers `pipeline_superseded`,
  not `pipeline_applied`: the writer that won converges the state, this one changed
  nothing.
- **The merge is re-checked, not trusted.** The branch can be edited after netCI opened it,
  so the designer's rules are applied again to what was read: a required stage removed,
  an unknown key, a custom stage without an anchor. A refusal is answered `200
  pipeline_rejected` with the reason and audited as `pipeline.merge_rejected`. It is not
  answered 4xx/5xx, because GitLab would retry a delivery that is already recorded, and the
  retry would be dropped as a duplicate. A merge into anything other than the default branch
  changes nothing.
- **The revision carries the module's own deployment targets unchanged.** The first version
  passed a key the module JSON does not have and would have written a revision with no
  targets (backend/tests/test_pipeline_designer.py pins this).
- **`customStages` is validated wherever a revision is written** (the Portal's delivery
  rules), not only by the designer: a script path other than `.netci/stages/<id>.sh` is
  refused (422 `INVALID_CUSTOM_STAGES`), not rewritten.
- **Module custom stages skip the catalog's second-administrator approval.** The merge
  request's review in the module repository is that approval, and the catalog's own stages
  still need it. A catalog id always wins over a module stage with the same id.
- **GitLab failing is 502/503, never GitLab's own status.** Its 401 is about netCI's token,
  not the caller. A script that cannot be read is an error, not an empty editor: saving the
  empty editor would commit an empty script over the real one.
- **The proposer never merges.** A proof script that opened the merge request and then
  merged it itself was refused by this session's own guard as self-approval -- which is the
  control this ADR relies on. `scripts/corp/e2e_designer.py` proposes and verifies; a person
  merges in between.
- Live so far (2026-09-26, corp lab): a proposal from netCI opened merge request !1 on
  `platform/payments-api` in the lab GitLab, branch `netci/pipeline-fe0b9835`, changing exactly
  `.netci/pipeline.yaml` and `.netci/stages/lint-dockerfile.sh`. Not yet verified: the merge
  webhook applying it (awaits a reviewer's merge).
