package io.netci.jenkins.homesync;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertInstanceOf;
import static org.junit.jupiter.api.Assertions.assertNotEquals;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertTrue;

import hudson.model.Item;
import hudson.model.User;
import hudson.security.HudsonPrivateSecurityRealm;
import hudson.security.csrf.DefaultCrumbIssuer;
import java.io.IOException;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.time.Duration;
import java.time.Instant;
import java.util.Base64;
import jenkins.model.Jenkins;
import net.sf.json.JSONObject;
import org.htmlunit.HttpMethod;
import org.htmlunit.WebRequest;
import org.htmlunit.http.Cookie;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.Test;
import org.jvnet.hudson.test.JenkinsRule;
import org.jvnet.hudson.test.MockAuthorizationStrategy;
import org.jvnet.hudson.test.junit.jupiter.WithJenkins;

/**
 * ADR-086: a crumb is accepted by every replica of the same sign-in, and by nothing else. "Another
 * replica" is a browser that holds only the shared cookie -- no session here -- which is what the
 * other JVM sees when the balancer retries a POST or the person's replica has died.
 */
@WithJenkins
class SharedCrumbTest {
    private static final String ALICE = "alice-password-0123456789";
    private static final String BOB = "bob-password-0123456789ab";

    static {
        try {
            Path key = Files.createTempFile("session", ".key");
            Files.writeString(key, "a-shared-session-key-for-the-test-0123456789");
            System.setProperty("netci.session.key.file", key.toString());
        } catch (IOException e) {
            throw new ExceptionInInitializerError(e);
        }
    }

    @AfterAll
    static void off() {
        System.clearProperty("netci.session.key.file");
    }

    private static void users(JenkinsRule j) throws Exception {
        HudsonPrivateSecurityRealm realm = new HudsonPrivateSecurityRealm(false, false, null);
        realm.createAccount("alice", ALICE);
        realm.createAccount("bob", BOB);
        j.jenkins.setSecurityRealm(realm);
        j.jenkins.setAuthorizationStrategy(new MockAuthorizationStrategy()
                .grant(Jenkins.READ, Item.READ, Item.CREATE).everywhere().to("alice", "bob"));
        // JenkinsRule installs a test issuer that accepts anything; this is the one under test.
        j.jenkins.setCrumbIssuer(new SharedCrumbIssuer());
    }

    private static String cookie(JenkinsRule.WebClient wc) {
        Cookie c = wc.getCookieManager().getCookie(SharedSession.COOKIE);
        return c == null || c.getValue().isEmpty() ? null : c.getValue();
    }

    /** The crumb a page served to this browser carries. */
    private static String crumb(JenkinsRule.WebClient wc) throws Exception {
        return JSONObject.fromObject(wc.goTo("crumbIssuer/api/json", "application/json").getWebResponse().getContentAsString()).getString("crumb");
    }

    /** A browser with only the shared cookie: the replica it reaches has never seen it. */
    private static JenkinsRule.WebClient elsewhere(JenkinsRule j, String value) throws IOException {
        JenkinsRule.WebClient wc = j.createWebClient();
        wc.setThrowExceptionOnFailingStatusCode(false);
        wc.getCookieManager().addCookie(new Cookie(j.getURL().getHost(), SharedSession.COOKIE, value, "/", null, false, true));
        return wc;
    }

    private static int create(JenkinsRule j, JenkinsRule.WebClient wc, String name, String crumb) throws Exception {
        WebRequest req = new WebRequest(new URL(j.getURL(), "createItem?name=" + name), HttpMethod.POST);
        req.setAdditionalHeader("Content-Type", "application/xml");
        if (crumb != null) {
            req.setAdditionalHeader("Jenkins-Crumb", crumb);
        }
        req.setRequestBody("<project/>");
        return wc.getPage(req).getWebResponse().getStatusCode();
    }

    @Test
    void aFormFromOneReplicaIsAcceptedByAnother(JenkinsRule j) throws Exception {
        users(j);
        JenkinsRule.WebClient here = j.createWebClient().login("alice", ALICE);
        String crumb = crumb(here);
        String value = cookie(here);
        assertNotNull(value);
        // The POST is the first request the other replica sees from this browser.
        assertEquals(200, create(j, elsewhere(j, value), "made-elsewhere", crumb));
        assertNotNull(j.jenkins.getItem("made-elsewhere"));
        assertEquals("alice", User.getById("alice", false).getId());
        // And on the replica that served the page, of course.
        here.setThrowExceptionOnFailingStatusCode(false);
        assertEquals(200, create(j, here, "made-here", crumb));
    }

    @Test
    void aCrumbIsBoundToItsSignInAndNothingElse(JenkinsRule j) throws Exception {
        users(j);
        JenkinsRule.WebClient alice = j.createWebClient().login("alice", ALICE);
        String aliceCrumb = crumb(alice);
        String aliceCookie = cookie(alice);
        JenkinsRule.WebClient bob = j.createWebClient().login("bob", BOB);
        String bobCookie = cookie(bob);

        assertEquals(403, create(j, elsewhere(j, bobCookie), "with-alices-crumb", aliceCrumb), "a crumb served to alice posted as bob");
        assertEquals(403, create(j, elsewhere(j, aliceCookie), "no-crumb", null), "no crumb at all");
        assertEquals(403, create(j, elsewhere(j, aliceCookie), "made-up", "0".repeat(64)), "a made-up crumb");
        assertNull(j.jenkins.getItem("with-alices-crumb"));

        // Signed out and in again: a new sign-in, and the crumb of the old one is worth nothing.
        alice.setThrowExceptionOnFailingStatusCode(false);
        alice.goTo("logout");
        JenkinsRule.WebClient again = j.createWebClient().login("alice", ALICE);
        String newCookie = cookie(again);
        assertNotEquals(sid(aliceCookie), sid(newCookie), "a new sign-in kept the old sign-in's id");
        assertEquals(403, create(j, elsewhere(j, newCookie), "old-crumb", aliceCrumb));
        assertEquals(200, create(j, elsewhere(j, newCookie), "new-crumb", crumb(again)));
    }

    @Test
    void aRenewedCookieKeepsItsSignInSoAnOpenPagesCrumbStaysValid(JenkinsRule j) throws Exception {
        users(j);
        JenkinsRule.WebClient wc = j.createWebClient().login("alice", ALICE);
        String crumb = crumb(wc);
        String first = cookie(wc);
        // Past half its lifetime (an hour): the next page renews it.
        SharedSession s = SharedSession.get();
        String old = s.issue(User.getById("alice", false), Instant.now().minus(Duration.ofMinutes(40)), sid(first));
        wc.getCookieManager().addCookie(new Cookie(j.getURL().getHost(), SharedSession.COOKIE, old, "/", null, false, true));
        wc.goTo("");
        String renewed = cookie(wc);
        assertNotEquals(old, renewed, "the cookie was not renewed");
        assertEquals(sid(first), sid(renewed));
        assertEquals(200, create(j, elsewhere(j, renewed), "after-renewal", crumb));
    }

    @Test
    void itReplacesOnlyJenkinsDefault(JenkinsRule j) {
        j.jenkins.setCrumbIssuer(new DefaultCrumbIssuer());
        SharedCrumbIssuer.install();
        assertInstanceOf(SharedCrumbIssuer.class, j.jenkins.getCrumbIssuer());
        // A site's own issuer is its decision (here JenkinsRule's test issuer).
        hudson.security.csrf.CrumbIssuer theirs = new org.jvnet.hudson.test.TestCrumbIssuer();
        j.jenkins.setCrumbIssuer(theirs);
        SharedCrumbIssuer.install();
        assertTrue(j.jenkins.getCrumbIssuer() == theirs);
    }

    private static String sid(String cookie) {
        String payload = cookie.substring(0, cookie.indexOf('.'));
        return JSONObject.fromObject(new String(Base64.getUrlDecoder().decode(payload), StandardCharsets.UTF_8)).getString("sid");
    }
}
