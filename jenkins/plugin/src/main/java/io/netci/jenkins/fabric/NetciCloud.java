package io.netci.jenkins.fabric;

import hudson.Extension;
import hudson.model.Descriptor;
import hudson.model.Label;
import hudson.model.Node;
import hudson.model.labels.LabelAtom;
import hudson.slaves.Cloud;
import hudson.slaves.NodeProvisioner;
import java.util.ArrayList;
import java.util.Collection;
import java.util.List;
import java.util.UUID;
import java.util.concurrent.CompletableFuture;
import java.util.logging.Level;
import java.util.logging.Logger;
import jenkins.model.Jenkins;
import jenkins.model.JenkinsLocationConfiguration;
import org.jenkinsci.Symbol;
import org.kohsuke.stapler.DataBoundConstructor;
import org.kohsuke.stapler.DataBoundSetter;

/**
 * Build sandboxes from netci-fabric (ADR-064): each provisioned agent is a warm sandbox claimed
 * for one build. The claim happens in {@link NetciLauncher}, so a node exists, and its inbound
 * secret with it, before the fabric is asked.
 */
public final class NetciCloud extends Cloud {
    private static final Logger LOGGER = Logger.getLogger(NetciCloud.class.getName());

    private String fabricUrl = "";
    private String tokenFile = "";
    private String controllerUrl = "";
    private int connectTimeoutSeconds = 300;
    private List<NetciPool> pools = new ArrayList<>();

    @DataBoundConstructor
    public NetciCloud(String name) {
        super(name);
    }

    public String getFabricUrl() {
        return fabricUrl;
    }

    @DataBoundSetter
    public void setFabricUrl(String fabricUrl) {
        this.fabricUrl = fabricUrl;
    }

    public String getTokenFile() {
        return tokenFile;
    }

    /** A file holding this cell's fabric token (mounted from a secret). The token never enters Jenkins' configuration. */
    @DataBoundSetter
    public void setTokenFile(String tokenFile) {
        this.tokenFile = tokenFile;
    }

    public String getControllerUrl() {
        return controllerUrl;
    }

    /** How sandboxes reach this controller; Jenkins' own URL if empty. */
    @DataBoundSetter
    public void setControllerUrl(String controllerUrl) {
        this.controllerUrl = controllerUrl;
    }

    public int getConnectTimeoutSeconds() {
        return connectTimeoutSeconds;
    }

    @DataBoundSetter
    public void setConnectTimeoutSeconds(int connectTimeoutSeconds) {
        this.connectTimeoutSeconds = connectTimeoutSeconds;
    }

    public List<NetciPool> getPools() {
        return pools;
    }

    @DataBoundSetter
    public void setPools(List<NetciPool> pools) {
        this.pools = pools == null ? new ArrayList<>() : new ArrayList<>(pools);
    }

    FabricClient client() {
        return new FabricClient(fabricUrl, tokenFile);
    }

    String controller() {
        if (controllerUrl != null && !controllerUrl.isBlank()) {
            return controllerUrl;
        }
        return JenkinsLocationConfiguration.get().getUrl();
    }

    NetciPool poolFor(Label label) {
        for (NetciPool p : pools) {
            if (label == null ? p.getLabelSet().isEmpty() : label.matches(p.getLabelSet())) {
                return p;
            }
        }
        return null;
    }

    @Override
    public boolean canProvision(CloudState state) {
        return poolFor(state.getLabel()) != null;
    }

    @Override
    public Collection<NodeProvisioner.PlannedNode> provision(CloudState state, int excessWorkload) {
        NetciPool pool = poolFor(state.getLabel());
        List<NodeProvisioner.PlannedNode> planned = new ArrayList<>();
        if (pool == null) {
            return planned;
        }
        for (int i = 0; i < excessWorkload; i++) {
            String name = "netci-" + pool.getPool() + "-" + UUID.randomUUID().toString().substring(0, 8);
            try {
                NetciAgent agent = new NetciAgent(name, getDisplayName(), pool.getPool(), pool.getLabels(), pool.getRemoteFs());
                planned.add(new NodeProvisioner.PlannedNode(name, CompletableFuture.completedFuture((Node) agent), 1));
            } catch (Descriptor.FormException | java.io.IOException e) {
                LOGGER.log(Level.WARNING, "cannot create a sandbox agent", e);
            }
        }
        return planned;
    }

    static NetciCloud named(String name) {
        Cloud c = Jenkins.get().clouds.getByName(name);
        return c instanceof NetciCloud n ? n : null;
    }

    /** One pool of the fabric and the labels it serves. */
    public static final class NetciPool extends hudson.model.AbstractDescribableImpl<NetciPool> {
        private final String pool;
        private final String labels;
        private String remoteFs = "/home/jenkins/agent";

        @DataBoundConstructor
        public NetciPool(String pool, String labels) {
            this.pool = pool;
            this.labels = labels;
        }

        public String getRemoteFs() {
            return remoteFs;
        }

        /** The agent's working directory: the sandbox's workspace volume (netci-sandbox's NETCI_WORKDIR). */
        @DataBoundSetter
        public void setRemoteFs(String remoteFs) {
            this.remoteFs = remoteFs == null || remoteFs.isBlank() ? "/home/jenkins/agent" : remoteFs;
        }

        public String getPool() {
            return pool;
        }

        public String getLabels() {
            return labels;
        }

        java.util.Set<LabelAtom> getLabelSet() {
            return Label.parse(labels);
        }

        @Extension
        public static final class DescriptorImpl extends Descriptor<NetciPool> {
            @Override
            public String getDisplayName() {
                return "netCI fabric pool";
            }
        }
    }

    @Extension
    @Symbol("netci")
    public static final class DescriptorImpl extends Descriptor<Cloud> {
        @Override
        public String getDisplayName() {
            return "netCI agent fabric";
        }
    }
}
