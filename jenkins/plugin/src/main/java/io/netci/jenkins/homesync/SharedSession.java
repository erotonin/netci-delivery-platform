package io.netci.jenkins.homesync;

import hudson.Extension;
import hudson.init.InitMilestone;
import hudson.init.Initializer;
import hudson.model.User;
import hudson.security.ACL;
import hudson.security.ACLContext;
import hudson.util.PluginServletFilter;
import jakarta.servlet.Filter;
import jakarta.servlet.FilterChain;
import jakarta.servlet.ServletException;
import jakarta.servlet.ServletRequest;
import jakarta.servlet.ServletResponse;
import jakarta.servlet.http.Cookie;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.security.MessageDigest;
import java.security.SecureRandom;
import java.time.Duration;
import java.time.Instant;
import java.util.Base64;
import java.util.logging.Level;
import java.util.logging.Logger;
import javax.crypto.Mac;
import javax.crypto.spec.SecretKeySpec;
import jenkins.security.SecurityListener;
import jenkins.security.seed.UserSeedProperty;
import net.sf.json.JSONObject;
import org.kohsuke.stapler.Stapler;
import org.kohsuke.stapler.StaplerRequest2;
import org.kohsuke.stapler.StaplerResponse2;
import org.springframework.security.core.Authentication;
import org.springframework.security.core.context.SecurityContext;
import org.springframework.security.core.context.SecurityContextHolder;
import org.springframework.security.web.context.HttpSessionSecurityContextRepository;

/**
 * A sign-in that survives its replica (ADR-077). Jenkins keeps a session in one JVM, so when a
 * replica died the person behind the balancer was anonymous on the other one (ADR-076 phase 2).
 *
 * <p>A signed-in browser session also gets {@code NETCI_SESSION}: the user's id, an expiry, and the
 * SHA-256 of the user's seed, signed with HMAC-SHA256 under a key every replica is given. A
 * replica that sees no session but a valid cookie signs the user in again, with the authorities
 * its security realm gives that user now -- the cookie carries no authority of its own. It is
 * refused when tampered with, expired, for a user that no longer exists, or after the user's seed
 * changed ("Invalidate sessions" in Jenkins, which is replicated with the user); sign-out clears it.
 *
 * <p>Each sign-in also has a random id ({@code sid}), kept when the cookie is renewed and new at
 * the next sign-in: the CSRF crumb is bound to it ({@link SharedCrumbIssuer}, ADR-086), as Jenkins
 * binds its own to the HTTP session -- which is one JVM's, so a crumb from one replica was refused
 * by the other.
 *
 * <p>Off unless {@code NETCI_SESSION_KEY_FILE} names a key of at least 32 bytes.
 */
public final class SharedSession implements Filter {
    private static final Logger LOGGER = Logger.getLogger(SharedSession.class.getName());
    static final String COOKIE = "NETCI_SESSION";
    /** The sign-in id of the cookie this request holds or was just given (for a crumb it renders). */
    static final String SID = SharedSession.class.getName() + ".sid";
    private static final SecureRandom RANDOM = new SecureRandom();

    private static volatile SharedSession instance;

    private final byte[] key;
    private final Duration ttl;

    SharedSession(byte[] key, Duration ttl) {
        this.key = key.clone();
        this.ttl = ttl;
    }

    static SharedSession get() {
        return instance;
    }

    @Initializer(after = InitMilestone.PLUGINS_STARTED)
    public static void register() throws ServletException {
        instance = null;
        String file = HomeSync.setting("NETCI_SESSION_KEY_FILE", "");
        if (file == null) {
            return;
        }
        byte[] key;
        try {
            key = Files.readString(Path.of(file), StandardCharsets.UTF_8).trim().getBytes(StandardCharsets.UTF_8);
        } catch (IOException e) {
            LOGGER.log(Level.SEVERE, "netCI: NETCI_SESSION_KEY_FILE is unreadable; sign-ins will not survive their replica", e);
            return;
        }
        if (key.length < 32) {
            LOGGER.severe("netCI: the shared session key is shorter than 32 bytes; refusing to sign sessions with it");
            return;
        }
        // Idle lifetime: renewed while the person is active, so it ends like a session does, not
        // like a "remember me" that lasts for days.
        long seconds = Long.parseLong(HomeSync.setting("NETCI_SESSION_TTL_SECONDS", "3600"));
        SharedSession s = new SharedSession(key, Duration.ofSeconds(seconds));
        instance = s;
        PluginServletFilter.addFilter(s);
    }

    /** The cookie value for a new sign-in of a user, valid until now + ttl. */
    String issue(User user, Instant now) {
        return issue(user, now, newSid());
    }

    /** The cookie value for a user's sign-in {@code sid}, valid until now + ttl. */
    String issue(User user, Instant now, String sid) {
        JSONObject claims = new JSONObject()
                .element("u", user.getId())
                .element("iat", now.getEpochSecond())
                .element("exp", now.plus(ttl).getEpochSecond())
                .element("s", seedHash(user))
                .element("sid", sid);
        String payload = b64(claims.toString().getBytes(StandardCharsets.UTF_8));
        return payload + "." + b64(mac(payload));
    }

    static String newSid() {
        byte[] b = new byte[18];
        RANDOM.nextBytes(b);
        return b64(b);
    }

    /** A cookie this key signed, still valid: whose it is, when it was issued, which sign-in ("" for a cookie from before sids). */
    record Claims(User user, long issuedAt, String sid) {}

    /** The user a cookie value names, or null if it is not one this key signed and still valid. */
    User verify(String value, Instant now) {
        Claims c = claims(value, now);
        return c == null ? null : c.user();
    }

    Claims claims(String value, Instant now) {
        int dot = value == null ? -1 : value.indexOf('.');
        if (dot <= 0) {
            return null;
        }
        String payload = value.substring(0, dot);
        byte[] supplied;
        try {
            supplied = Base64.getUrlDecoder().decode(value.substring(dot + 1));
        } catch (IllegalArgumentException e) {
            return null;
        }
        if (!MessageDigest.isEqual(mac(payload), supplied)) {
            return null;
        }
        JSONObject claims;
        try {
            claims = JSONObject.fromObject(new String(Base64.getUrlDecoder().decode(payload), StandardCharsets.UTF_8));
        } catch (RuntimeException e) {
            return null;
        }
        long epoch = now.getEpochSecond();
        if (claims.optLong("exp") <= epoch || claims.optLong("iat") > epoch + 60) {
            return null;
        }
        User user = User.getById(claims.optString("u"), false);
        if (user == null || !MessageDigest.isEqual(seedHash(user).getBytes(StandardCharsets.UTF_8),
                claims.optString("s").getBytes(StandardCharsets.UTF_8))) {
            return null;
        }
        return new Claims(user, claims.optLong("iat"), claims.optString("sid"));
    }

    private static String seedHash(User user) {
        UserSeedProperty seed = user.getProperty(UserSeedProperty.class);
        return HomeSync.sha256((seed == null || seed.getSeed() == null ? "" : seed.getSeed())
                .getBytes(StandardCharsets.UTF_8));
    }

    private byte[] mac(String payload) {
        try {
            Mac m = Mac.getInstance("HmacSHA256");
            m.init(new SecretKeySpec(key, "HmacSHA256"));
            return m.doFinal(payload.getBytes(StandardCharsets.US_ASCII));
        } catch (java.security.GeneralSecurityException e) {
            throw new IllegalStateException(e);
        }
    }

    private static String b64(byte[] b) {
        return Base64.getUrlEncoder().withoutPadding().encodeToString(b);
    }

    /** The Set-Cookie header (Servlet 5 has no SameSite, so it is written out). */
    static String setCookie(HttpServletRequest req, String value, long maxAge) {
        String path = req.getContextPath().isEmpty() ? "/" : req.getContextPath();
        return COOKIE + "=" + value + "; Path=" + path + "; Max-Age=" + maxAge + "; HttpOnly; SameSite=Lax"
                + (req.isSecure() ? "; Secure" : "");
    }

    /**
     * Anonymous with a valid cookie: the sign-in is restored. Signed in through a browser session
     * (not an API token: those have no session and get no cookie): the cookie is issued, or renewed
     * once half its lifetime has passed. Issued here rather than at sign-in because the sign-in
     * request is answered by the security filters before any of Jenkins' own code sees it.
     */
    @Override
    public void doFilter(ServletRequest request, ServletResponse response, FilterChain chain) throws IOException, ServletException {
        HttpServletRequest req = (HttpServletRequest) request;
        HttpServletResponse rsp = (HttpServletResponse) response;
        // Signing out must end it everywhere: cleared before Jenkins handles the request, and never
        // re-issued by it -- otherwise the next page would sign the person straight back in.
        if ("/logout".equals(req.getRequestURI().substring(req.getContextPath().length()))) {
            rsp.addHeader("Set-Cookie", setCookie(req, "", 0));
            chain.doFilter(request, response);
            return;
        }
        String value = cookieValue(req);
        if (ACL.isAnonymous2(jenkins.model.Jenkins.getAuthentication2())) {
            if (value != null) {
                restore(req, rsp, value);
            }
        } else if (req.getSession(false) != null) {
            User current = User.current();
            if (current != null) {
                Instant now = Instant.now();
                Claims held;
                try (ACLContext ignored = ACL.as2(ACL.SYSTEM2)) {
                    held = value == null ? null : claims(value, now);
                }
                // The same sign-in keeps its sid through renewals (a crumb in an open page stays valid);
                // anything else is a new sign-in.
                boolean same = held != null && held.user().getId().equals(current.getId()) && !held.sid().isEmpty();
                String sid = same ? held.sid() : newSid();
                if (!same || held.issuedAt() < now.minus(ttl.dividedBy(2)).getEpochSecond()) {
                    rsp.addHeader("Set-Cookie", setCookie(req, issue(current, now, sid), ttl.toSeconds()));
                }
                req.setAttribute(SID, sid);
            }
        }
        chain.doFilter(request, response);
    }

    /**
     * What this request's crumb is bound to: the user's id and the sign-in's sid, or null when the
     * request has no shared sign-in (an API token, a cookie from before sids, the feature off) and
     * Jenkins' own crumb applies. The request's own user, or -- signed in nowhere yet -- the
     * cookie's: the filter that restores a sign-in from the cookie may run after Jenkins' CSRF
     * filter, and a POST that is the first request on this replica (a retry from the balancer) is
     * checked before it. A cookie for someone else than the request's user binds nothing.
     */
    static String crumbBinding(ServletRequest request) {
        SharedSession s = instance;
        if (s == null || !(request instanceof HttpServletRequest req)) {
            return null;
        }
        Authentication a = jenkins.model.Jenkins.getAuthentication2();
        boolean anonymous = ACL.isAnonymous2(a);
        Object given = req.getAttribute(SID);
        User current = anonymous ? null : User.current();
        if (given instanceof String sid && current != null) {
            return current.getId() + ";" + sid;
        }
        String value = cookieValue(req);
        if (value == null) {
            return null;
        }
        Claims c;
        try (ACLContext ignored = ACL.as2(ACL.SYSTEM2)) {
            c = s.claims(value, Instant.now());
        }
        if (c == null || c.sid().isEmpty() || (!anonymous && (current == null || !current.getId().equals(c.user().getId())))) {
            return null;
        }
        return c.user().getId() + ";" + c.sid();
    }

    private static String cookieValue(HttpServletRequest req) {
        if (req.getCookies() != null) {
            for (Cookie c : req.getCookies()) {
                if (COOKIE.equals(c.getName()) && !c.getValue().isEmpty()) {
                    return c.getValue();
                }
            }
        }
        return null;
    }

    private void restore(HttpServletRequest req, HttpServletResponse rsp, String value) {
        User user;
        Authentication auth;
        try (ACLContext ignored = ACL.as2(ACL.SYSTEM2)) {
            user = verify(value, Instant.now());
            if (user == null) {
                rsp.addHeader("Set-Cookie", setCookie(req, "", 0)); // one that does not verify is dropped, not retried each request
                return;
            }
            auth = user.impersonate2(); // the realm's current authorities, not anything the cookie says
        } catch (RuntimeException e) {
            LOGGER.log(Level.FINE, "netCI: a shared session could not be restored", e);
            return;
        }
        SecurityContext ctx = SecurityContextHolder.createEmptyContext();
        ctx.setAuthentication(auth);
        SecurityContextHolder.setContext(ctx);
        req.getSession(true).setAttribute(HttpSessionSecurityContextRepository.SPRING_SECURITY_CONTEXT_KEY, ctx);
        LOGGER.fine(() -> "netCI: restored the sign-in of " + user.getId() + " from its shared session");
    }

    /** Clears the cookie at sign-out. */
    @Extension
    public static final class Clear extends SecurityListener {
        @Override
        protected void loggedOut(String username) {
            SharedSession s = SharedSession.get();
            StaplerRequest2 req = Stapler.getCurrentRequest2();
            StaplerResponse2 rsp = Stapler.getCurrentResponse2();
            if (s != null && req != null && rsp != null) {
                rsp.addHeader("Set-Cookie", setCookie(req, "", 0));
            }
        }
    }
}
