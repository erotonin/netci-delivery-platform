package io.netci.jenkins;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertTrue;

import hudson.model.FreeStyleBuild;
import hudson.model.FreeStyleProject;
import hudson.model.Item;
import hudson.model.ParametersAction;
import hudson.model.ParametersDefinitionProperty;
import hudson.model.StringParameterDefinition;
import hudson.model.User;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.List;
import java.util.UUID;
import java.util.concurrent.Callable;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import jenkins.model.Jenkins;
import net.sf.json.JSONObject;
import org.htmlunit.HttpMethod;
import org.htmlunit.Page;
import org.htmlunit.WebRequest;
import org.jenkinsci.plugins.workflow.cps.CpsFlowDefinition;
import org.jenkinsci.plugins.workflow.job.WorkflowJob;
import org.jenkinsci.plugins.workflow.job.WorkflowRun;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.jvnet.hudson.test.JenkinsRule;
import org.jvnet.hudson.test.MockAuthorizationStrategy;
import org.jvnet.hudson.test.SleepBuilder;
import org.jvnet.hudson.test.junit.jupiter.WithJenkins;

@WithJenkins
class NetciEndpointTest {
    private JenkinsRule r;

    @BeforeEach
    void setUp(JenkinsRule r) {
        this.r = r;
        r.jenkins.setSecurityRealm(r.createDummySecurityRealm());
        r.jenkins.setAuthorizationStrategy(new MockAuthorizationStrategy()
                .grant(Jenkins.READ, Item.READ, Item.BUILD).everywhere().to("builder")
                .grant(Jenkins.READ, Item.READ).everywhere().to("reader")
                .grant(Jenkins.READ).everywhere().to("nobody"));
    }

    record Answer(int status, JSONObject body) {}

    private final java.util.Map<String, String> tokens = new java.util.concurrent.ConcurrentHashMap<>();

    /** One API token per user, made before any concurrent use: generating tokens races. */
    private synchronized String token(String user) throws Exception {
        String t = tokens.get(user);
        if (t == null) {
            t = User.getById(user, true).getProperty(jenkins.security.ApiTokenProperty.class)
                    .generateNewToken("test").plainValue;
            tokens.put(user, t);
        }
        return t;
    }

    private Answer call(String user, HttpMethod method, String path, String body) throws Exception {
        JenkinsRule.WebClient wc = r.createWebClient().withBasicCredentials(user, token(user));
        wc.setThrowExceptionOnFailingStatusCode(false);
        WebRequest req = new WebRequest(new URL(r.getURL(), path), method);
        if (body != null) {
            req.setAdditionalHeader("Content-Type", "application/json");
            req.setRequestBody(body);
            req.setCharset(StandardCharsets.UTF_8);
        }
        Page page = wc.getPage(req);
        String text = page.getWebResponse().getContentAsString();
        JSONObject json = text.startsWith("{") ? JSONObject.fromObject(text) : new JSONObject();
        return new Answer(page.getWebResponse().getStatusCode(), json);
    }

    private Answer dispatch(String user, String job, String runId, String params) throws Exception {
        return call(user, HttpMethod.POST, "netci/dispatch",
                "{\"job\":\"" + job + "\",\"runId\":\"" + runId + "\",\"requestedBy\":\"alice\""
                        + (params == null ? "" : ",\"parameters\":" + params) + "}");
    }

    private static String run() {
        return UUID.randomUUID().toString();
    }

    @Test
    void theSameRunDispatchedAgainIsTheSameQueueItem() throws Exception {
        FreeStyleProject p = r.createFreeStyleProject("held");
        p.setQuietPeriod(600); // stays queued for the whole test
        String id = run();
        Answer first = dispatch("builder", "held", id, null);
        assertEquals(200, first.status(), first.body().toString());
        assertEquals("queued", first.body().getString("state"));
        assertTrue(first.body().getBoolean("created"));
        Answer again = dispatch("builder", "held", id, null);
        assertEquals("queued", again.body().getString("state"));
        assertFalse(again.body().getBoolean("created"));
        assertEquals(first.body().getLong("queueId"), again.body().getLong("queueId"));
        assertEquals(1, r.jenkins.getQueue().getItems().length);
        assertEquals(first.body().getString("session"), again.body().getString("session"));
    }

    @Test
    void twoRunsWithEqualParametersAreNotFoldedIntoOne() throws Exception {
        FreeStyleProject p = r.createFreeStyleProject("held");
        p.setQuietPeriod(600);
        assertTrue(dispatch("builder", "held", run(), null).body().getBoolean("created"));
        assertTrue(dispatch("builder", "held", run(), null).body().getBoolean("created"));
        assertEquals(2, r.jenkins.getQueue().getItems().length);
    }

    @Test
    void aStartedRunIsFoundAgainAndAfterTheControllerReloadsFromDisk() throws Exception {
        r.createFreeStyleProject("quick");
        String id = run();
        assertTrue(dispatch("builder", "quick", id, null).body().getBoolean("created"));
        r.waitUntilNoActivity();
        Answer again = dispatch("builder", "quick", id, null);
        assertEquals("started", again.body().getString("state"));
        assertFalse(again.body().getBoolean("created"));
        assertEquals(1, again.body().getJSONObject("build").getInt("number"));
        assertEquals("SUCCESS", again.body().getJSONObject("build").getString("result"));

        r.jenkins.reload(); // what a restart reads back: the action must be in build.xml
        Answer afterReload = dispatch("builder", "quick", id, null);
        assertEquals("started", afterReload.body().getString("state"), afterReload.body().toString());
        assertEquals(1, r.jenkins.getItemByFullName("quick", FreeStyleProject.class).getBuilds().size());
        Answer lookup = call("reader", HttpMethod.GET, "netci/run?job=quick&runId=" + id, null);
        assertEquals(200, lookup.status());
        assertEquals(1, lookup.body().getJSONObject("build").getInt("number"));
    }

    @Test
    void aRunningBuildIsNeverStartedAgain() throws Exception {
        FreeStyleProject p = r.createFreeStyleProject("slow");
        p.setQuietPeriod(0);
        p.getBuildersList().add(new SleepBuilder(3000));
        String id = run();
        dispatch("builder", "slow", id, null);
        FreeStyleBuild b = null;
        for (int i = 0; i < 100 && (b = p.getLastBuild()) == null; i++) {
            Thread.sleep(50);
        }
        assertNotNull(b);
        Answer during = dispatch("builder", "slow", id, null);
        assertFalse(during.body().getBoolean("created"));
        assertTrue(List.of("starting", "started").contains(during.body().getString("state")));
        r.waitUntilNoActivity();
        assertEquals(1, p.getBuilds().size());
    }

    @Test
    void concurrentDispatchesOfOneRunScheduleItOnce() throws Exception {
        FreeStyleProject p = r.createFreeStyleProject("held");
        p.setQuietPeriod(600);
        String id = run();
        token("builder");
        ExecutorService pool = Executors.newFixedThreadPool(16);
        List<Future<Answer>> answers = new ArrayList<>();
        for (int i = 0; i < 16; i++) {
            Callable<Answer> c = () -> dispatch("builder", "held", id, null);
            answers.add(pool.submit(c));
        }
        int created = 0;
        for (Future<Answer> f : answers) {
            Answer a = f.get();
            assertEquals(200, a.status(), a.body().toString());
            created += a.body().getBoolean("created") ? 1 : 0;
        }
        pool.shutdown();
        assertEquals(1, created);
        assertEquals(1, r.jenkins.getQueue().getItems().length);
    }

    @Test
    void parametersAreTheJobsOwnAndAnythingElseIsRefused() throws Exception {
        FreeStyleProject p = r.createFreeStyleProject("params");
        p.addProperty(new ParametersDefinitionProperty(
                new StringParameterDefinition("A", "a0"), new StringParameterDefinition("B", "b0")));
        Answer ok = dispatch("builder", "params", run(), "{\"A\":\"a1\"}");
        assertEquals(200, ok.status(), ok.body().toString());
        r.waitUntilNoActivity();
        ParametersAction pa = p.getLastBuild().getAction(ParametersAction.class);
        assertEquals("a1", pa.getParameter("A").getValue());
        assertEquals("b0", pa.getParameter("B").getValue());

        assertEquals(400, dispatch("builder", "params", run(), "{\"NOT_DECLARED\":\"x\"}").status());
        assertEquals(400, dispatch("builder", "params", run(), "{\"A\":1}").status());
        assertEquals(400, dispatch("builder", "params", run(), "[]").status());
        r.createFreeStyleProject("plain");
        assertEquals(400, dispatch("builder", "plain", run(), "{\"A\":\"x\"}").status());
        r.waitUntilNoActivity();
        assertEquals(1, p.getBuilds().size());
        assertEquals(0, r.jenkins.getItemByFullName("plain", FreeStyleProject.class).getBuilds().size());
    }

    @Test
    void theCallerNeedsBuildPermissionAndAPost() throws Exception {
        FreeStyleProject p = r.createFreeStyleProject("guarded");
        p.setQuietPeriod(600);
        assertEquals(403, dispatch("reader", "guarded", run(), null).status());
        assertEquals(404, dispatch("nobody", "guarded", run(), null).status());
        Answer get = call("builder", HttpMethod.GET, "netci/dispatch", null);
        assertTrue(get.status() >= 400, "GET status " + get.status());
        assertEquals(0, r.jenkins.getQueue().getItems().length);
    }

    @Test
    void malformedRequestsAreRefused() throws Exception {
        r.createFreeStyleProject("p");
        assertEquals(400, dispatch("builder", "p", "not-a-uuid", null).status());
        assertEquals(400, dispatch("builder", "p", run().toUpperCase(), null).status());
        assertEquals(400, call("builder", HttpMethod.POST, "netci/dispatch", "nonsense").status());
        assertEquals(400, call("builder", HttpMethod.POST, "netci/dispatch", "{\"job\":\"p\",\"runId\":\"" + run() + "\",\"x\":\"" + "a".repeat(70000) + "\"}").status());
        Answer absent = call("builder", HttpMethod.GET, "netci/run?job=p&runId=" + run(), null);
        assertEquals(404, absent.status());
        assertEquals("absent", absent.body().getString("state"));
        assertEquals(0, r.jenkins.getQueue().getItems().length);
    }

    @Test
    void aPipelineRunCarriesItsRunId() throws Exception {
        WorkflowJob p = r.jenkins.createProject(WorkflowJob.class, "pipe");
        p.setDefinition(new CpsFlowDefinition("echo 'hello'", true));
        String id = run();
        assertTrue(dispatch("builder", "pipe", id, null).body().getBoolean("created"));
        r.waitUntilNoActivity();
        WorkflowRun b = p.getLastBuild();
        assertNotNull(b);
        assertEquals(id, b.getAction(NetciRunAction.class).getRunId());
        assertTrue(b.getCauses().stream().anyMatch(c -> c instanceof NetciCause n && n.getShortDescription().contains("alice")));
        Answer again = dispatch("builder", "pipe", id, null);
        assertEquals("started", again.body().getString("state"));
        assertEquals(1, p.getBuilds().size());
    }
}
