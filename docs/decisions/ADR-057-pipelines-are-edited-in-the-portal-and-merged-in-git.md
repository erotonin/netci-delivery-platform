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
- **The merge is re-checked, not trusted.** The branch can be edited after netCI opened it,
  so on merge netCI reads `.netci/pipeline.yaml` from GitLab *at the merge commit* (never
  from the webhook body) and applies the designer's rules again: a required stage removed,
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
- Not verified live: no merge request has been opened or merged against a real GitLab yet.
  The lab GitLab (infra/corp/gitlab) exists, but netCI is not yet installed on `netci-corp`.
