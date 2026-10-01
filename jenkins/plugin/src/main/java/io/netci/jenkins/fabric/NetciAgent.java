package io.netci.jenkins.fabric;

import hudson.Extension;
import hudson.model.Descriptor;
import hudson.model.Node;
import hudson.model.TaskListener;
import hudson.slaves.AbstractCloudComputer;
import hudson.slaves.AbstractCloudSlave;
import java.io.IOException;
import java.util.logging.Level;
import java.util.logging.Logger;
import org.jenkinsci.plugins.durabletask.executors.OnceRetentionStrategy;

/** An agent running in a fabric sandbox, for one build. */
public final class NetciAgent extends AbstractCloudSlave {
    private static final long serialVersionUID = 1L;
    private static final Logger LOGGER = Logger.getLogger(NetciAgent.class.getName());

    private final String cloudName;
    private final String pool;
    /** The fabric's claim; kept with the node, so it is released even after a restart. */
    private volatile String claimId;

    NetciAgent(String name, String cloudName, String pool, String labels, String remoteFs)
            throws Descriptor.FormException, IOException {
        super(name, remoteFs, new NetciLauncher());
        this.cloudName = cloudName;
        this.pool = pool;
        setNumExecutors(1);
        setMode(Node.Mode.EXCLUSIVE);
        setLabelString(labels);
        // One build, then the node is removed (and the sandbox with it). Idle a minute at most.
        setRetentionStrategy(new OnceRetentionStrategy(1));
    }

    String getCloudName() {
        return cloudName;
    }

    String getPool() {
        return pool;
    }

    String getClaimId() {
        return claimId;
    }

    void setClaimId(String claimId) {
        this.claimId = claimId;
    }

    @Override
    public AbstractCloudComputer<NetciAgent> createComputer() {
        return new AbstractCloudComputer<>(this);
    }

    @Override
    protected void _terminate(TaskListener listener) throws IOException, InterruptedException {
        String id = claimId;
        NetciCloud cloud = NetciCloud.named(cloudName);
        if (id == null || cloud == null) {
            return;
        }
        try {
            cloud.client().release(id);
        } catch (IOException e) {
            // The fabric gives up a claim never bound, and deletes a sandbox whose agent is gone;
            // a release that fails here costs a sandbox for that long, never a build.
            LOGGER.log(Level.WARNING, "could not release sandbox claim " + id + " of " + getNodeName(), e);
        }
    }

    @Extension
    public static final class DescriptorImpl extends SlaveDescriptor {
        @Override
        public String getDisplayName() {
            return "netCI sandbox agent";
        }

        @Override
        public boolean isInstantiable() {
            return false;
        }
    }
}
