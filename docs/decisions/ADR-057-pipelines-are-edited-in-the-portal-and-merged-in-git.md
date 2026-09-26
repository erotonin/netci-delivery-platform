# ADR-057: Pipelines are edited in the portal and merged in git

Status: Proposed.

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
