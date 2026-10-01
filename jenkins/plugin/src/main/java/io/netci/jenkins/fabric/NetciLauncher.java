package io.netci.jenkins.fabric;

import hudson.model.TaskListener;
import hudson.slaves.JNLPLauncher;
import hudson.slaves.SlaveComputer;
import java.io.IOException;
import java.util.concurrent.TimeUnit;
import jenkins.model.Jenkins;

/**
 * Claims a sandbox for the agent and waits for it to connect (inbound, over WebSocket). The
 * claim carries the node's inbound secret: the sandbox can become this agent and nothing else.
 */
public final class NetciLauncher extends JNLPLauncher {
    public NetciLauncher() {
        super();
        setWebSocket(true);
    }

    @Override
    public boolean isLaunchSupported() {
        return true;
    }

    @Override
    public void launch(SlaveComputer computer, TaskListener listener) {
        if (!(computer.getNode() instanceof NetciAgent agent)) {
            return;
        }
        NetciCloud cloud = NetciCloud.named(agent.getCloudName());
        try {
            if (cloud == null) {
                throw new IOException("cloud " + agent.getCloudName() + " no longer exists");
            }
            String controller = cloud.controller();
            if (controller == null || controller.isBlank()) {
                throw new IOException("neither the cloud's controllerUrl nor Jenkins' URL is set: a sandbox could not reach this controller");
            }
            if (agent.getClaimId() == null) {
                FabricClient.Claim claim = cloud.client().claim(agent.getPool(), agent.getNodeName(), computer.getJnlpMac(), controller);
                agent.setClaimId(claim.id());
                // Persist the claim with the node, unless the node was removed meanwhile.
                if (Jenkins.get().getNode(agent.getNodeName()) == agent) {
                    Jenkins.get().updateNode(agent);
                }
                listener.getLogger().printf("netCI: sandbox %s (%s) claimed for %s%n", claim.pod(), claim.warm() ? "warm" : "created for this claim", agent.getNodeName());
            }
            long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(cloud.getConnectTimeoutSeconds());
            while (!computer.isOnline()) {
                if (System.nanoTime() > deadline) {
                    throw new IOException("the sandbox did not connect within " + cloud.getConnectTimeoutSeconds() + " s");
                }
                Thread.sleep(200);
            }
        } catch (IOException | InterruptedException e) {
            listener.error("netCI: " + e.getMessage());
            try {
                agent.terminate(); // releases the claim; Jenkins provisions again if still needed
            } catch (IOException | InterruptedException ignored) {
                // terminate logs its own failures
            }
            if (e instanceof InterruptedException) {
                Thread.currentThread().interrupt();
            }
        }
    }
}
