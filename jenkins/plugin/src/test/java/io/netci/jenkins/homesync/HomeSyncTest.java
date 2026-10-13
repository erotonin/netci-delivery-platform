package io.netci.jenkins.homesync;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.io.IOException;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;
import java.util.function.BooleanSupplier;
import org.htmlunit.HttpMethod;
import org.htmlunit.Page;
import org.htmlunit.WebRequest;
import org.jenkinsci.plugins.workflow.job.WorkflowJob;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.Test;
import org.jvnet.hudson.test.JenkinsRule;
import org.jvnet.hudson.test.junit.jupiter.WithJenkins;

@WithJenkins
class HomeSyncTest {
    static final FakeHomeSync STORE;

    static {
        try {
            STORE = new FakeHomeSync();
            Path token = Files.createTempFile("homesync", ".token");
            Files.writeString(token, FakeHomeSync.TOKEN);
            System.setProperty("netci.homesync.url", STORE.url());
            System.setProperty("netci.homesync.token.file", token.toString());
        } catch (IOException e) {
            throw new ExceptionInInitializerError(e);
        }
    }

    @AfterEach
    void clean() {
        STORE.reset();
    }

    private static void await(String what, BooleanSupplier ok) throws InterruptedException {
        long end = System.currentTimeMillis() + 15_000;
        while (!ok.getAsBoolean()) {
            if (System.currentTimeMillis() > end) {
                throw new AssertionError("timed out waiting: " + what);
            }
            Thread.sleep(50);
        }
    }

    private static void ready() throws InterruptedException {
        await("the home to catch up", () -> HomeSync.get() != null && HomeSync.get().ready());
    }

    private static String stored(String path) {
        synchronized (STORE) {
            FakeHomeSync.Entry e = STORE.items.get(path);
            return e == null || e.content() == null ? null : new String(e.content(), StandardCharsets.UTF_8);
        }
    }

    private static long storedVersion(String path) {
        synchronized (STORE) {
            FakeHomeSync.Entry e = STORE.items.get(path);
            return e == null ? 0 : e.version();
        }
    }

    @Test
    void aJobCreatedAndSavedHereIsPublishedVersionByVersion(JenkinsRule j) throws Exception {
        ready();
        WorkflowJob job = j.createProject(WorkflowJob.class, "app");
        await("the create published", () -> storedVersion("app") >= 1);
        long before = storedVersion("app");
        job.setDescription("from here");
        await("the save published", () -> stored("app") != null && stored("app").contains("from here"));
        assertEquals(before + 1, storedVersion("app"));
        // Each change was made from the version this home held: never blind.
        List<FakeHomeSync.Seen> puts = STORE.seen().stream().filter(s -> s.path().equals("app")).toList();
        for (int i = 0; i < puts.size(); i++) {
            assertEquals(i, puts.get(i).ifMatch(), "change " + i + " of app");
        }
    }

    @Test
    void anotherReplicasChangeIsAppliedHereWithoutBeingPublishedBack(JenkinsRule j) throws Exception {
        ready();
        STORE.changeFrom("r1", "made-on-r1", "<flow-definition plugin=\"workflow-job\"><description>from r1</description>"
                + "<keepDependencies>false</keepDependencies><properties/><triggers/><disabled>false</disabled></flow-definition>");
        await("r1's job here", () -> j.jenkins.getItemByFullName("made-on-r1") != null);
        WorkflowJob job = j.jenkins.getItemByFullName("made-on-r1", WorkflowJob.class);
        assertEquals("from r1", job.getDescription());
        Thread.sleep(500);
        assertTrue(STORE.seen().stream().noneMatch(s -> s.path().equals("made-on-r1")), "the applied change was published back");

        STORE.changeFrom("r1", "made-on-r1", null);
        await("r1's deletion here", () -> j.jenkins.getItemByFullName("made-on-r1") == null);
    }

    @Test
    void anEditThroughTheUiIsOrderedByTheItemsLock(JenkinsRule j) throws Exception {
        ready();
        j.createProject(WorkflowJob.class, "locked-edit");
        await("published", () -> storedVersion("locked-edit") >= 1);
        JenkinsRule.WebClient wc = j.createWebClient();
        WebRequest req = new WebRequest(new URL(j.getURL(), "job/locked-edit/submitDescription"), HttpMethod.POST);
        req.setRequestParameters(List.of(new org.htmlunit.util.NameValuePair("description", "through the UI")));
        wc.addCrumb(req);
        wc.getPage(req);
        await("published", () -> stored("locked-edit") != null && stored("locked-edit").contains("through the UI"));
        FakeHomeSync.Seen last = STORE.seen().stream().filter(s -> s.path().equals("locked-edit")).reduce((a, b) -> b).orElseThrow();
        assertTrue(last.lockToken() > 0, "the UI edit was published without the item's lock");
        assertNull(STORE.lockHolder.get("locked-edit"), "the lock was not released");
    }

    @Test
    void anEditIsRefusedWhileAnotherReplicaHoldsTheItem(JenkinsRule j) throws Exception {
        ready();
        WorkflowJob job = j.createProject(WorkflowJob.class, "contended");
        job.setDescription("before");
        await("published", () -> stored("contended") != null && stored("contended").contains("before"));
        STORE.lockAs("r1", "contended");
        JenkinsRule.WebClient wc = j.createWebClient();
        wc.setThrowExceptionOnFailingStatusCode(false);
        WebRequest req = new WebRequest(new URL(j.getURL(), "job/contended/submitDescription"), HttpMethod.POST);
        req.setRequestParameters(List.of(new org.htmlunit.util.NameValuePair("description", "racing r1")));
        wc.addCrumb(req);
        Page page = wc.getPage(req);
        assertEquals(409, page.getWebResponse().getStatusCode());
        assertTrue(page.getWebResponse().getContentAsString().contains("being changed on replica r1"));
        assertEquals("before", job.getDescription(), "a refused edit changed the job");
    }

    @Test
    void withoutTheStoreEditsAreRefusedAndReadsStillWork(JenkinsRule j) throws Exception {
        ready();
        j.createProject(WorkflowJob.class, "offline");
        await("published", () -> storedVersion("offline") >= 1);
        STORE.down = true;
        JenkinsRule.WebClient wc = j.createWebClient();
        wc.setThrowExceptionOnFailingStatusCode(false);
        assertEquals(200, wc.goTo("job/offline/").getWebResponse().getStatusCode());
        WebRequest req = new WebRequest(new URL(j.getURL(), "job/offline/disable"), HttpMethod.POST);
        wc.addCrumb(req);
        Page page = wc.getPage(req);
        assertEquals(503, page.getWebResponse().getStatusCode());
        assertTrue(!j.jenkins.getItemByFullName("offline", WorkflowJob.class).isDisabled(), "an unordered edit was applied");
    }

    @Test
    void aBackgroundSaveThatLostToAnotherReplicaTakesTheSharedVersion(JenkinsRule j) throws Exception {
        ready();
        WorkflowJob job = j.createProject(WorkflowJob.class, "bg");
        await("published", () -> storedVersion("bg") >= 1);
        // r1 changed it and this home has not applied that yet (the feed entry is held back by
        // changing the store directly, as a slow follower would see it).
        synchronized (STORE) {
            long v = STORE.items.get("bg").version() + 1;
            STORE.items.put("bg", new FakeHomeSync.Entry(v, ("<flow-definition plugin=\"workflow-job\"><description>r1 won"
                    + "</description><keepDependencies>false</keepDependencies><properties/><triggers/>"
                    + "<disabled>false</disabled></flow-definition>").getBytes(StandardCharsets.UTF_8), "r1"));
        }
        job.setDescription("lost here"); // a save no request ordered, made from the stale version
        await("the shared version applied", () -> "r1 won".equals(j.jenkins.getItemByFullName("bg", WorkflowJob.class).getDescription()));
        assertNotNull(stored("bg"));
        assertTrue(stored("bg").contains("r1 won"), "the stale save replaced r1's");
    }
}
