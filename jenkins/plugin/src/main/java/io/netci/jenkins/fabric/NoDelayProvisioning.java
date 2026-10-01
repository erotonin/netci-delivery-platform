package io.netci.jenkins.fabric;

import hudson.Extension;
import hudson.model.Label;
import hudson.model.LoadStatistics;
import hudson.slaves.Cloud;
import hudson.slaves.CloudProvisioningListener;
import hudson.slaves.NodeProvisioner;
import java.util.Collection;
import jenkins.model.Jenkins;

/**
 * Provisions netCI sandboxes as soon as a build waits, instead of after Jenkins' default
 * smoothing (which waits to see whether the load persists). A warm sandbox costs nothing to
 * take, and the point of keeping it warm is that nobody waits.
 */
@Extension(ordinal = 100)
public final class NoDelayProvisioning extends NodeProvisioner.Strategy {
    @Override
    public NodeProvisioner.StrategyDecision apply(NodeProvisioner.StrategyState state) {
        Label label = state.getLabel();
        LoadStatistics.LoadStatisticsSnapshot snap = state.getSnapshot();
        int available = snap.getAvailableExecutors() + snap.getConnectingExecutors()
                + state.getPlannedCapacitySnapshot() + state.getAdditionalPlannedCapacity();
        int demand = snap.getQueueLength();
        if (available < demand) {
            Cloud.CloudState cs = new Cloud.CloudState(label, state.getAdditionalPlannedCapacity());
            for (Cloud c : Jenkins.get().clouds) {
                if (!(c instanceof NetciCloud) || !c.canProvision(cs)) {
                    continue;
                }
                Collection<NodeProvisioner.PlannedNode> planned = c.provision(cs, demand - available);
                for (CloudProvisioningListener l : CloudProvisioningListener.all()) {
                    l.onStarted(c, label, planned);
                }
                state.recordPendingLaunches(planned);
                for (NodeProvisioner.PlannedNode p : planned) {
                    available += p.numExecutors;
                }
                if (available >= demand) {
                    break;
                }
            }
        }
        return available >= demand
                ? NodeProvisioner.StrategyDecision.PROVISIONING_COMPLETED
                : NodeProvisioner.StrategyDecision.CONSULT_REMAINING_STRATEGIES;
    }
}
