package io.netci.jenkins;

import hudson.model.Cause;
import org.kohsuke.stapler.export.Exported;

/** Shown on the build: who asked netCI for this run. Display only; netCI decided and audited it. */
public final class NetciCause extends Cause {
    private final String runId;
    private final String requestedBy;

    public NetciCause(String runId, String requestedBy) {
        this.runId = runId;
        this.requestedBy = requestedBy;
    }

    @Exported(visibility = 3)
    public String getRunId() {
        return runId;
    }

    @Exported(visibility = 3)
    public String getRequestedBy() {
        return requestedBy;
    }

    @Override
    public String getShortDescription() {
        return requestedBy == null || requestedBy.isEmpty()
                ? "Started by netCI run " + runId
                : "Started by netCI run " + runId + " for " + requestedBy;
    }

    @Override
    public boolean equals(Object o) {
        return o instanceof NetciCause c && c.runId.equals(runId);
    }

    @Override
    public int hashCode() {
        return runId.hashCode();
    }
}
