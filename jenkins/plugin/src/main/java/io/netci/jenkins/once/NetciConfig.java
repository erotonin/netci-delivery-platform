package io.netci.jenkins.once;

import hudson.Extension;
import jenkins.model.GlobalConfiguration;
import org.jenkinsci.Symbol;
import org.kohsuke.stapler.DataBoundSetter;

/**
 * Where netci-queue is, for {@code netciOnce} (ADR-065). The token stays in a mounted file and
 * never enters Jenkins' configuration. JCasC: {@code unclassified: netci: {queueUrl, tokenFile}}.
 */
@Extension
@Symbol("netci")
public final class NetciConfig extends GlobalConfiguration {
    private String queueUrl = "";
    private String tokenFile = "";
    private int retrySeconds = 60;

    public NetciConfig() {
        load();
    }

    public static NetciConfig get() {
        return GlobalConfiguration.all().get(NetciConfig.class);
    }

    public String getQueueUrl() {
        return queueUrl;
    }

    @DataBoundSetter
    public void setQueueUrl(String queueUrl) {
        this.queueUrl = queueUrl == null ? "" : queueUrl;
        save();
    }

    public String getTokenFile() {
        return tokenFile;
    }

    @DataBoundSetter
    public void setTokenFile(String tokenFile) {
        this.tokenFile = tokenFile == null ? "" : tokenFile;
        save();
    }

    /** How long a guarded block waits for netci-queue before it fails (fail closed). */
    public int getRetrySeconds() {
        return retrySeconds;
    }

    @DataBoundSetter
    public void setRetrySeconds(int retrySeconds) {
        this.retrySeconds = retrySeconds;
        save();
    }
}
