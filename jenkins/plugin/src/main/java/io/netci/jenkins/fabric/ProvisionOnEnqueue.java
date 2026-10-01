package io.netci.jenkins.fabric;

import hudson.Extension;
import hudson.model.Label;
import hudson.model.Queue;
import hudson.model.queue.QueueListener;
import hudson.slaves.Cloud;
import jenkins.model.Jenkins;

/**
 * Wakes the label's provisioner the moment a build for a fabric pool becomes buildable. Without
 * it the provisioner looks only on its own period (2 s on netCI's controllers, 10 s by default),
 * and in the lab that was most of the time between a build starting and its sandbox being claimed.
 */
@Extension
public final class ProvisionOnEnqueue extends QueueListener {
    @Override
    public void onEnterBuildable(Queue.BuildableItem item) {
        Label label = item.getAssignedLabel();
        if (label == null) {
            return;
        }
        Cloud.CloudState state = new Cloud.CloudState(label, 0);
        for (Cloud c : Jenkins.get().clouds) {
            if (c instanceof NetciCloud && c.canProvision(state)) {
                label.nodeProvisioner.suggestReviewNow();
                return;
            }
        }
    }
}
