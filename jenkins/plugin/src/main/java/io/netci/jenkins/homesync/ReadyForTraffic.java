package io.netci.jenkins.homesync;

import hudson.Extension;
import hudson.init.InitMilestone;
import hudson.model.UnprotectedRootAction;
import java.io.File;
import jenkins.model.Jenkins;
import org.kohsuke.stapler.HttpResponse;
import org.kohsuke.stapler.HttpResponses;

/**
 * The balancer's health check (ADR-080): 200 while this replica takes traffic, 503 while it starts
 * or drains (with the drain agent, ADR-089, only while it starts).
 *
 * <p>Draining is a file, created by the pod's preStop hook before Jenkins is told to stop: the
 * balancer marks the replica down within its check interval and sends everyone to the other one,
 * so nothing is in flight when Jenkins stops. Checked on /login before, a replica stopping still
 * looked up until Jenkins itself went away, and the requests in flight then were cut (a rolling
 * restart: 2 of 297 polls, "remote end closed connection without response"). Unprotected: the
 * balancer has no credentials, and the answer says nothing but "ready", "starting" or "draining".
 */
@Extension
public final class ReadyForTraffic implements UnprotectedRootAction {
    @Override
    public String getUrlName() {
        return "netci-ready";
    }

    @Override
    public String getIconFileName() {
        return null;
    }

    @Override
    public String getDisplayName() {
        return null;
    }

    static File drainFile() {
        return new File(HomeSync.setting("NETCI_DRAIN_FILE", "/tmp/netci-draining"));
    }

    public HttpResponse doIndex() {
        // With the drain agent (ADR-089) the balancer learns of a drain from it, and this stays up
        // until Jenkins is gone: a 503 here would mark the replica down, and the balancer would
        // close every request in flight on it.
        if (drainFile().exists() && !DrainAgent.on()) {
            return HttpResponses.status(503); // the body is left out: a status is all the balancer reads
        }
        if (Jenkins.get().getInitLevel() != InitMilestone.COMPLETED) {
            return HttpResponses.status(503);
        }
        // Not until this home has caught up with the store: before that it refuses edits and
        // knows of no other replica's runs, so traffic belongs on the replica that does.
        HomeSync sync = HomeSync.get();
        if (sync != null && !sync.ready()) {
            return HttpResponses.status(503);
        }
        return HttpResponses.text("ready");
    }

    /** How many requests this replica is serving: the pod's preStop waits for 0 before Jenkins stops. */
    public HttpResponse doBusy() {
        return HttpResponses.text(Integer.toString(DrainAgent.inFlight()));
    }
}
