"""One projection of a pipeline run, for every route that answers with one.

The Portal's module views had a copy that omitted what a run *was* -- whether it deployed, only
built or only verified, what started it, its version tag, its admission -- so a build-only run
read "deploys to dev" and a production release, which builds nothing, read "manual build".
"""

from __future__ import annotations

from ..domain.models import PipelineRun


def pipeline_run_json(item: PipelineRun) -> dict[str, object]:
    parameters = dict(item.parameters)
    return {
        "id": str(item.id),
        "applicationId": str(item.application_id),
        "status": item.status.value,
        "commitSha": item.commit_sha,
        "branch": item.branch,
        "environment": item.environment.value,
        "parameters": parameters,
        "correlationId": item.correlation_id,
        "jenkinsRunId": item.jenkins_run_id,
        "workflowId": item.workflow_id,
        "artifactDigest": item.artifact_digest,
        "consoleUrl": item.console_url,
        "retryOf": str(item.retry_of) if item.retry_of else None,
        "configRevisionId": str(item.config_revision_id) if item.config_revision_id else None,
        "startedBy": item.started_by,
        "deployAfterBuild": item.deploy_after_build,
        "publishArtifact": item.publish_artifact,
        "releaseTag": item.release_tag,
        "trigger": dict(item.trigger),
        "admittedAt": item.admitted_at.isoformat() if item.admitted_at else None,
        "concurrencyGroup": item.concurrency_group,
        "supersededBy": str(item.superseded_by) if item.superseded_by else None,
        "ciFinishedAt": item.ci_finished_at.isoformat() if item.ci_finished_at else None,
        # A run a production request made to release an artifact another run built: it builds
        # nothing, and says which run it took the digest from.
        "promotedFrom": str(parameters["sourcePipelineRunId"]) if parameters.get("sourcePipelineRunId") else None,
        "productionRequestId": str(parameters["productionRequestId"]).split(":", 1)[0] if parameters.get("productionRequestId") else None,
        "createdAt": item.created_at.isoformat(),
        "updatedAt": item.updated_at.isoformat(),
    }
