package io.netci.jenkins;

import hudson.model.Action;
import hudson.model.Computer;
import hudson.model.Executor;
import hudson.model.Job;
import hudson.model.Queue;
import hudson.model.Run;
import hudson.model.queue.WorkUnit;
import java.util.List;
import jenkins.model.Jenkins;

/**
 * Where a netCI run is in this controller, if anywhere.
 *
 * <p>Call {@link #find} holding the queue lock ({@link Queue#withLock}). An item travels from the
 * queue (waiting, blocked, buildable, pending) to an executor's work unit -- under that lock --
 * and only then becomes a build. Looking in all three places under the lock means a run that
 * exists is never missed while it moves between them, which is what lets a dispatch that finds
 * nothing schedule the run without ever starting it twice.
 */
final class RunLookup {
    enum State {
        QUEUED,
        /** An executor has taken the item and the build is being created. */
        STARTING,
        STARTED
    }

    record Found(State state, long queueId, Run<?, ?> build) {}

    /** Recent builds searched when the caller gives no bound. */
    static final int MAX_BUILDS = 1000;

    private RunLookup() {}

    /**
     * @param notBeforeMillis builds scheduled before this are not searched (0: no bound but
     *     {@link #MAX_BUILDS}); the caller passes a margin before it first accepted the run
     */
    static Found find(Job<?, ?> job, String runId, long notBeforeMillis) {
        Jenkins j = Jenkins.get();
        for (Queue.Item item : j.getQueue().getItems()) {
            if (item.task == job && matches(item.getAllActions(), runId)) {
                return new Found(State.QUEUED, item.getId(), null);
            }
        }
        for (Computer c : j.getComputers()) {
            for (Executor e : c.getAllExecutors()) {
                Queue.Executable exec = e.getCurrentExecutable();
                if (exec instanceof Run<?, ?> r && r.getParent() == job && runIdOf(r, runId)) {
                    return new Found(State.STARTED, -1, r);
                }
                WorkUnit wu = e.getCurrentWorkUnit();
                if (wu != null && wu.context.task == job && matches(wu.context.actions, runId)) {
                    return new Found(State.STARTING, -1, null);
                }
            }
        }
        int seen = 0;
        for (Run<?, ?> r : job.getBuilds()) {
            if (++seen > MAX_BUILDS || (notBeforeMillis > 0 && r.getTimeInMillis() < notBeforeMillis)) {
                break;
            }
            if (runIdOf(r, runId)) {
                return new Found(State.STARTED, -1, r);
            }
        }
        return null;
    }

    private static boolean runIdOf(Run<?, ?> r, String runId) {
        NetciRunAction a = r.getAction(NetciRunAction.class);
        return a != null && a.getRunId().equals(runId);
    }

    private static boolean matches(List<? extends Action> actions, String runId) {
        for (Action a : actions) {
            if (a instanceof NetciRunAction n && n.getRunId().equals(runId)) {
                return true;
            }
        }
        return false;
    }
}
