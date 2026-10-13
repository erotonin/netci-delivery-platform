package io.netci.jenkins.homesync;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertTrue;

import hudson.model.User;
import hudson.security.HudsonPrivateSecurityRealm;
import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.time.Duration;
import java.time.Instant;
import jenkins.model.Jenkins;
import jenkins.security.seed.UserSeedProperty;
import net.sf.json.JSONObject;
import org.htmlunit.http.Cookie;
import org.junit.jupiter.api.Test;
import org.jvnet.hudson.test.JenkinsRule;
import org.jvnet.hudson.test.MockAuthorizationStrategy;
import org.jvnet.hudson.test.junit.jupiter.WithJenkins;

@WithJenkins
class SharedSessionTest {
    private static final String PASSWORD = "alice-password-0123456789";

    static {
        try {
            Path key = Files.createTempFile("session", ".key");
            Files.writeString(key, "a-shared-session-key-for-the-test-0123456789");
            System.setProperty("netci.session.key.file", key.toString());
        } catch (IOException e) {
            throw new ExceptionInInitializerError(e);
        }
    }

    private static User alice(JenkinsRule j) throws Exception {
        HudsonPrivateSecurityRealm realm = new HudsonPrivateSecurityRealm(false, false, null);
        realm.createAccount("alice", PASSWORD);
        j.jenkins.setSecurityRealm(realm);
        j.jenkins.setAuthorizationStrategy(new MockAuthorizationStrategy().grant(Jenkins.READ).everywhere().to("alice"));
        return User.getById("alice", false);
    }

    private static String sessionCookie(JenkinsRule.WebClient wc) {
        Cookie c = wc.getCookieManager().getCookie(SharedSession.COOKIE);
        return c == null || c.getValue().isEmpty() ? null : c.getValue();
    }

    /** A browser with no session on this replica: only the shared cookie, as after a failover. */
    private static String whoWithOnly(JenkinsRule j, String value) throws Exception {
        JenkinsRule.WebClient fresh = j.createWebClient();
        if (value != null) {
            fresh.getCookieManager().addCookie(new Cookie(j.getURL().getHost(), SharedSession.COOKIE, value, "/", null, false, true));
        }
        String body = fresh.goTo("whoAmI/api/json", "application/json").getWebResponse().getContentAsString();
        JSONObject who = JSONObject.fromObject(body);
        return who.optBoolean("anonymous") ? "anonymous" : who.optString("name");
    }

    @Test
    void aSignInSurvivesOnAReplicaThatNeverSawIt(JenkinsRule j) throws Exception {
        alice(j);
        JenkinsRule.WebClient wc = j.createWebClient().login("alice", PASSWORD);
        String value = sessionCookie(wc);
        assertNotNull(value, "signing in set no shared session");
        assertEquals("alice", whoWithOnly(j, value));
        assertEquals("anonymous", whoWithOnly(j, null));
    }

    @Test
    void aTamperedOrForeignCookieSignsNobodyIn(JenkinsRule j) throws Exception {
        alice(j);
        String value = sessionCookie(j.createWebClient().login("alice", PASSWORD));
        String[] parts = value.split("\\.");
        // The claims rewritten to another user, the signature kept.
        String forged = java.util.Base64.getUrlEncoder().withoutPadding().encodeToString(
                new String(java.util.Base64.getUrlDecoder().decode(parts[0])).replace("alice", "admin").getBytes()) + "." + parts[1];
        assertEquals("anonymous", whoWithOnly(j, forged));
        assertEquals("anonymous", whoWithOnly(j, parts[0] + ".AAAA"));
        assertEquals("anonymous", whoWithOnly(j, "garbage"));
        SharedSession other = new SharedSession("another-key-another-key-another-key-0123".getBytes(), Duration.ofHours(8));
        assertEquals("anonymous", whoWithOnly(j, other.issue(User.getById("alice", false), Instant.now())));
    }

    @Test
    void itExpiresAndIsRevokedWithTheUsersSeed(JenkinsRule j) throws Exception {
        User alice = alice(j);
        SharedSession s = SharedSession.get();
        assertNotNull(s);
        Instant now = Instant.now();
        assertNotNull(s.verify(s.issue(alice, now), now));
        assertNull(s.verify(s.issue(alice, now.minus(Duration.ofHours(9))), now), "an expired session was accepted");
        assertNull(s.verify(s.issue(alice, now.plus(Duration.ofHours(1))), now), "a session issued in the future was accepted");

        String before = s.issue(alice, now);
        alice.getProperty(UserSeedProperty.class).renewSeed(); // Jenkins' "Invalidate sessions"
        assertNull(s.verify(before, now), "a session outlived the user's seed");
        assertNotNull(s.verify(s.issue(alice, now), now));

        String last = s.issue(alice, now);
        alice.delete();
        assertNull(s.verify(last, now), "a deleted user's session was accepted");
    }

    @Test
    void signingOutClearsIt(JenkinsRule j) throws Exception {
        alice(j);
        JenkinsRule.WebClient wc = j.createWebClient().login("alice", PASSWORD);
        assertNotNull(sessionCookie(wc));
        wc.setThrowExceptionOnFailingStatusCode(false);
        wc.goTo("logout");
        assertNull(sessionCookie(wc), "signing out left the shared session");
        // whoAmI answers anyone; what matters is who it says this browser is now.
        JSONObject who = JSONObject.fromObject(wc.goTo("whoAmI/api/json", "application/json").getWebResponse().getContentAsString());
        assertTrue(who.optBoolean("anonymous"), "the next page signed the person back in: " + who);
    }
}
