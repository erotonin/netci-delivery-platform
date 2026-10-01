package io.netci.jenkins;

import hudson.model.Action;
import hudson.model.InvisibleAction;
import hudson.model.Queue;
import java.io.Serializable;
import java.util.List;
import org.kohsuke.stapler.export.Exported;
import org.kohsuke.stapler.export.ExportedBean;

/**
 * The netCI run a queue item or a build belongs to. Jenkins copies a queue item's actions onto
 * the build it becomes and saves them in the build's record, so a run can be found again after
 * the controller restarts.
 *
 * <p>As a {@link Queue.QueueAction} it also keeps Jenkins from folding two different runs with
 * equal parameters into one queue item (its default), and makes the same run scheduled twice
 * resolve to the item already queued.
 */
@ExportedBean
public final class NetciRunAction extends InvisibleAction implements Queue.QueueAction, Serializable {
    private static final long serialVersionUID = 1L;

    private final String runId;

    public NetciRunAction(String runId) {
        this.runId = runId;
    }

    @Exported
    public String getRunId() {
        return runId;
    }

    /** Schedule a new item unless the other one is this same run. */
    @Override
    public boolean shouldSchedule(List<Action> actions) {
        for (Action a : actions) {
            if (a instanceof NetciRunAction other && other.runId.equals(runId)) {
                return false;
            }
        }
        return true;
    }
}
