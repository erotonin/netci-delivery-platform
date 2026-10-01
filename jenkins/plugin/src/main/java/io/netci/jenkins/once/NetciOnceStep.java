package io.netci.jenkins.once;

import hudson.AbortException;
import hudson.Extension;
import hudson.model.Run;
import hudson.model.TaskListener;
import java.io.IOException;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.time.Duration;
import java.util.Set;
import java.util.UUID;
import jenkins.model.Jenkins;
import jenkins.util.Timer;
import net.sf.json.JSONObject;
import org.jenkinsci.plugins.workflow.steps.AbstractStepExecutionImpl;
import org.jenkinsci.plugins.workflow.steps.BodyExecutionCallback;
import org.jenkinsci.plugins.workflow.steps.Step;
import org.jenkinsci.plugins.workflow.steps.StepContext;
import org.jenkinsci.plugins.workflow.steps.StepDescriptor;
import org.jenkinsci.plugins.workflow.steps.StepExecution;
import org.kohsuke.stapler.DataBoundConstructor;
import org.kohsuke.stapler.DataBoundSetter;

/**
 * {@code netciOnce('deploy-prod') { ... }}: the body runs once per build, even if the controller
 * loses its last second of state and the Pipeline starts the block again after a takeover
 * (ADR-065). The record is kept by netci-queue in PostgreSQL, outside JENKINS_HOME.
 */
public final class NetciOnceStep extends Step {
    private final String key;
    private boolean retry;

    @DataBoundConstructor
    public NetciOnceStep(String key) {
        this.key = key;
    }

    public String getKey() {
        return key;
    }

    public boolean isRetry() {
        return retry;
    }

    /** A person checked the target and runs the block again, knowing it started before. */
    @DataBoundSetter
    public void setRetry(boolean retry) {
        this.retry = retry;
    }

    @Override
    public StepExecution start(StepContext context) {
        return new Execution(context, key, retry);
    }

    static final class Execution extends AbstractStepExecutionImpl {
        private static final long serialVersionUID = 1L;
        private static final HttpClient HTTP = HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(3)).build();

        private final String key;
        private final boolean retry;
        /** This attempt. Saved with the execution: after a rollback the step starts anew, with a new one. */
        private final String nonce = UUID.randomUUID().toString();

        Execution(StepContext context, String key, boolean retry) {
            super(context);
            this.key = key;
            this.retry = retry;
        }

        @Override
        public boolean start() throws Exception {
            Run<?, ?> run = getContext().get(Run.class);
            TaskListener listener = getContext().get(TaskListener.class);
            String scope = Jenkins.get().getLegacyInstanceId() + "/" + run.getParent().getFullName() + "#" + run.getNumber();
            if (retry) {
                listener.getLogger().printf("netciOnce('%s'): retry requested -- running the block without checking whether it ran before%n", key);
                body();
                return false;
            }
            // Off the Pipeline's thread: the call may wait for netci-queue for up to a minute.
            Timer.get().submit(() -> {
                try {
                    JSONObject answer = record(scope);
                    if (answer.optBoolean("first")) {
                        listener.getLogger().printf("netciOnce('%s'): first start in this build -- running it%n", key);
                        body();
                    } else {
                        getContext().onFailure(new AbortException(String.format(
                                "netciOnce('%s'): this block already started in this build at %s, under a state the controller no longer has "
                                        + "(a takeover after a power loss). Not running it again. Check the target, then run it with "
                                        + "netciOnce(key: '%s', retry: true).", key, answer.optString("recordedAt"), key)));
                    }
                } catch (Exception e) {
                    getContext().onFailure(e);
                }
            });
            return false;
        }

        private void body() {
            getContext().newBodyInvoker().withCallback(BodyExecutionCallback.wrap(getContext())).start();
        }

        /** Asks netci-queue; retries what it cannot know, and fails closed after the configured time. */
        private JSONObject record(String scope) throws Exception {
            NetciConfig cfg = NetciConfig.get();
            if (cfg.getQueueUrl().isBlank() || cfg.getTokenFile().isBlank()) {
                throw new AbortException("netciOnce: netci-queue is not configured (unclassified: netci: queueUrl, tokenFile); "
                        + "a block that must not run twice does not run unguarded");
            }
            String body = new JSONObject().element("scope", scope).element("key", key).element("nonce", nonce).toString();
            long deadline = System.nanoTime() + Duration.ofSeconds(cfg.getRetrySeconds()).toNanos();
            String last = "";
            while (true) {
                try {
                    String token = Files.readString(Path.of(cfg.getTokenFile()), StandardCharsets.UTF_8).trim();
                    HttpResponse<String> r = HTTP.send(HttpRequest.newBuilder(URI.create(cfg.getQueueUrl().replaceAll("/+$", "") + "/v1/once"))
                                    .timeout(Duration.ofSeconds(5))
                                    .header("Authorization", "Bearer " + token)
                                    .header("Content-Type", "application/json")
                                    .POST(HttpRequest.BodyPublishers.ofString(body, StandardCharsets.UTF_8))
                                    .build(),
                            HttpResponse.BodyHandlers.ofString());
                    if (r.statusCode() == 200 || r.statusCode() == 409) {
                        return JSONObject.fromObject(r.body());
                    }
                    if (r.statusCode() == 400 || r.statusCode() == 401 || r.statusCode() == 403) {
                        throw new AbortException("netciOnce: netci-queue refused the record (HTTP " + r.statusCode() + "): " + r.body());
                    }
                    last = "HTTP " + r.statusCode();
                } catch (IOException e) {
                    if (e instanceof AbortException) {
                        throw e;
                    }
                    last = e.toString();
                }
                if (System.nanoTime() > deadline) {
                    throw new AbortException("netciOnce('" + key + "'): cannot tell whether this block ran before (" + last
                            + "); not running it");
                }
                Thread.sleep(2000);
            }
        }

        @Override
        public void onResume() {
            // Resumed, not started: the record was made when it started, and the body goes on.
        }
    }

    @Extension
    public static final class DescriptorImpl extends StepDescriptor {
        @Override
        public String getFunctionName() {
            return "netciOnce";
        }

        @Override
        public String getDisplayName() {
            return "Run a block at most once per build, across controller takeovers";
        }

        @Override
        public boolean takesImplicitBlockArgument() {
            return true;
        }

        @Override
        public Set<? extends Class<?>> getRequiredContext() {
            return Set.of(Run.class, TaskListener.class);
        }
    }
}
