package io.netci.jenkins.homesync;

import hudson.init.InitMilestone;
import hudson.init.Initializer;
import hudson.model.Job;
import hudson.security.ACL;
import hudson.security.ACLContext;
import hudson.util.PluginServletFilter;
import jakarta.servlet.Filter;
import jakarta.servlet.FilterChain;
import jakarta.servlet.ServletException;
import jakarta.servlet.ServletRequest;
import jakarta.servlet.ServletResponse;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import hudson.model.User;
import java.io.IOException;
import java.net.CookieManager;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Duration;
import java.net.URLDecoder;
import java.net.URLEncoder;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.logging.Level;
import java.util.logging.Logger;
import jenkins.model.Jenkins;

/**
 * Sends a request about a job's builds to the job's owner (ADR-078): "Build now" on a replica that
 * does not own the job, and the pages of a build this replica does not have (it is running on the
 * owner). Without this, a "Build now" on the wrong replica was declined and said nothing.
 *
 * <p>Through the balancer: the answer sets its cookie to the owner's and redirects (307 for a POST,
 * so the request is kept). Once only: a request already redirected that lands here again (the
 * owner is down, the balancer sent it back) is answered 409 with the reason, never redirected in a
 * loop. {@code NETCI_REPLICA_COOKIES} maps replicas to the balancer's cookie values
 * ({@code jenkins-0=r0,jenkins-1=r1}); {@code NETCI_REPLICA_NAME} (or {@code POD_NAME}) is this one.
 */
public final class OwnerRouting implements Filter {
    private static final Logger LOGGER = Logger.getLogger(OwnerRouting.class.getName());
    static final String ROUTED = "netci-routed";

    private final String me;
    private final String cookieName;
    private final Map<String, String> cookies;
    private final Map<String, String> urls;

    OwnerRouting(String me, String cookieName, Map<String, String> cookies, Map<String, String> urls) {
        this.me = me;
        this.cookieName = cookieName;
        this.cookies = cookies;
        this.urls = urls;
    }

    static Map<String, String> pairs(String list) {
        Map<String, String> m = new HashMap<>();
        for (String pair : list == null ? new String[0] : list.split(",")) {
            String[] kv = pair.trim().split("=", 2);
            if (kv.length == 2) {
                m.put(kv[0].trim(), kv[1].trim());
            }
        }
        return m;
    }

    /** This replica's name, as the store knows it from its credential (the pod's name in the lab). */
    static String me() {
        String me = HomeSync.setting("NETCI_REPLICA_NAME", HomeSync.setting("POD_NAME", ""));
        return me == null ? "" : me;
    }

    @Initializer(after = InitMilestone.PLUGINS_STARTED)
    public static void register() throws ServletException {
        String me = HomeSync.setting("NETCI_REPLICA_NAME", HomeSync.setting("POD_NAME", ""));
        String map = HomeSync.setting("NETCI_REPLICA_COOKIES", "");
        if (HomeSync.setting("NETCI_HOMESYNC_URL", "") == null || me == null || map == null) {
            return;
        }
        PluginServletFilter.addFilter(new OwnerRouting(me, HomeSync.setting("NETCI_BALANCER_COOKIE", "NETCI_REPLICA"), pairs(map),
                pairs(HomeSync.setting("NETCI_REPLICA_URLS", ""))));
    }

    /** A request about a job's builds: the job's full name, and whether it starts a build or reads one. */
    record Target(String job, boolean starts, int number) {}

    static Target target(String method, String path) {
        String[] parts = path.split("/");
        List<String> names = new ArrayList<>();
        int i = 0;
        while (i < parts.length && parts[i].isEmpty()) {
            i++;
        }
        while (i + 1 < parts.length && "job".equals(parts[i])) {
            names.add(URLDecoder.decode(parts[i + 1], StandardCharsets.UTF_8));
            i += 2;
        }
        if (names.isEmpty() || i >= parts.length) {
            return null;
        }
        String job = String.join("/", names);
        if ("POST".equals(method) && i == parts.length - 1 && (parts[i].equals("build") || parts[i].equals("buildWithParameters"))) {
            return new Target(job, true, 0);
        }
        if ("GET".equals(method) && parts[i].matches("[1-9][0-9]{0,8}")) {
            return new Target(job, false, Integer.parseInt(parts[i]));
        }
        return null;
    }

    @Override
    public void doFilter(ServletRequest request, ServletResponse response, FilterChain chain) throws IOException, ServletException {
        HttpServletRequest req = (HttpServletRequest) request;
        HttpServletResponse rsp = (HttpServletResponse) response;
        HomeSync sync = HomeSync.get();
        Target t = sync == null || !sync.ready() ? null : target(req.getMethod(), req.getRequestURI().substring(req.getContextPath().length()));
        if (t == null || (!t.starts() && hasBuild(t))) {
            chain.doFilter(request, response);
            return;
        }
        String owner;
        try {
            HomeSyncClient.JobOwner o = sync.client.jobOwner(t.job());
            owner = o.live() ? o.owner() : null;
        } catch (IOException e) {
            LOGGER.log(Level.FINE, "netCI homesync: cannot learn the owner of " + t.job(), e);
            chain.doFilter(request, response);
            return;
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            chain.doFilter(request, response);
            return;
        }
        if (owner == null || owner.equals(me)) {
            chain.doFilter(request, response); // nobody holds it live (this replica may take it), or it is ours
            return;
        }
        if (t.starts()) {
            forward(t, owner, req, rsp);
            return;
        }
        String value = cookies.get(owner);
        if (value == null || req.getParameter(ROUTED) != null) {
            rsp.setStatus(409);
            rsp.setContentType("text/plain;charset=UTF-8");
            rsp.getWriter().println("netCI: " + t.job() + (t.starts() ? " builds" : " #" + t.number() + " runs") + " on replica " + owner
                    + ", which this request could not be sent to; try again in a moment");
            return;
        }
        String location = location(req.getContextPath(), req.getRequestURI().substring(req.getContextPath().length()), req.getQueryString());
        rsp.addHeader("Set-Cookie", cookieName + "=" + value + "; Path=/; HttpOnly; SameSite=Lax" + (req.isSecure() ? "; Secure" : ""));
        rsp.setStatus(t.starts() ? 307 : 302);
        rsp.setHeader("Location", location);
    }

    /**
     * The same request, marked as routed. Every path segment and query pair is decoded and encoded
     * again, so nothing the client sent reaches the Location header as it was written (no CR/LF).
     */
    static String location(String contextPath, String path, String rawQuery) {
        StringBuilder out = new StringBuilder(contextPath);
        for (String segment : path.split("/")) {
            if (!segment.isEmpty()) {
                out.append('/').append(URLEncoder.encode(URLDecoder.decode(segment, StandardCharsets.UTF_8), StandardCharsets.UTF_8)
                        .replace("+", "%20"));
            }
        }
        if (path.endsWith("/")) {
            out.append('/');
        }
        out.append('?');
        if (rawQuery != null) {
            for (String pair : rawQuery.split("&")) {
                int eq = pair.indexOf('=');
                String k = URLDecoder.decode(eq < 0 ? pair : pair.substring(0, eq), StandardCharsets.UTF_8);
                String v = eq < 0 ? "" : URLDecoder.decode(pair.substring(eq + 1), StandardCharsets.UTF_8);
                if (!k.isEmpty() && !k.equals(ROUTED)) {
                    out.append(URLEncoder.encode(k, StandardCharsets.UTF_8)).append('=')
                            .append(URLEncoder.encode(v, StandardCharsets.UTF_8)).append('&');
                }
            }
        }
        return out.append(ROUTED).append("=1").toString();
    }

    /**
     * "Build now" for a job another replica owns is made there, by this replica, for this person.
     * Not by redirecting the browser: its POST carries a CSRF crumb bound to this replica's session,
     * and the owner refused it (403) -- the lab's first try. This replica has already checked the
     * crumb; it acts for the same person on the owner with the same credential (an API token, valid
     * on every replica since users are replicated) or a shared session it signs for them (ADR-077),
     * takes the owner's own crumb, and relays the owner's answer.
     */
    private void forward(Target t, String owner, HttpServletRequest req, HttpServletResponse rsp) throws IOException {
        String base = urls.get(owner);
        String auth = req.getHeader("Authorization");
        String cookie = null;
        if (auth == null) {
            SharedSession shared = SharedSession.get();
            User user = User.current();
            if (shared != null && user != null) {
                cookie = SharedSession.COOKIE + "=" + shared.issue(user, java.time.Instant.now());
            }
        }
        if (base == null || req.getParameter(ROUTED) != null || (auth == null && cookie == null)) {
            refuse(rsp, t, owner, auth == null && cookie == null ? "this replica cannot act for an anonymous request there" : null);
            return;
        }
        CookieManager jar = new CookieManager();
        HttpClient http = HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(3)).cookieHandler(jar).build();
        String ctx = req.getContextPath();
        try {
            HttpRequest.Builder crumbReq = HttpRequest.newBuilder(URI.create(base + ctx + "/crumbIssuer/api/json")).timeout(Duration.ofSeconds(10));
            credentials(crumbReq, auth, cookie);
            HttpResponse<String> crumb = http.send(crumbReq.GET().build(), HttpResponse.BodyHandlers.ofString());
            StringBuilder form = new StringBuilder();
            for (Map.Entry<String, String[]> e : req.getParameterMap().entrySet()) {
                if (e.getKey().equals("Jenkins-Crumb") || e.getKey().equals(ROUTED)) {
                    continue;
                }
                for (String v : e.getValue()) {
                    form.append(URLEncoder.encode(e.getKey(), StandardCharsets.UTF_8)).append('=')
                            .append(URLEncoder.encode(v, StandardCharsets.UTF_8)).append('&');
                }
            }
            form.append(ROUTED).append("=1");
            HttpRequest.Builder post = HttpRequest.newBuilder(URI.create(base + encodedPath(ctx, req.getRequestURI().substring(ctx.length()))))
                    .timeout(Duration.ofSeconds(15)).header("Content-Type", "application/x-www-form-urlencoded");
            credentials(post, auth, cookie);
            if (crumb.statusCode() == 200) {
                net.sf.json.JSONObject c = net.sf.json.JSONObject.fromObject(crumb.body());
                post.header(c.getString("crumbRequestField"), c.getString("crumb"));
            }
            HttpResponse<String> answer = http.send(post.POST(HttpRequest.BodyPublishers.ofString(form.toString())).build(),
                    HttpResponse.BodyHandlers.ofString());
            rsp.setStatus(answer.statusCode());
            rsp.setHeader("X-Netci-Built-On", owner);
            answer.headers().firstValue("Location").ifPresent(l -> rsp.setHeader("Location", relative(l)));
            rsp.setContentType("text/plain;charset=UTF-8");
            rsp.getWriter().print(answer.statusCode() < 300 ? "netCI: scheduled on replica " + owner + "\n" : answer.body());
        } catch (IOException e) {
            LOGGER.log(Level.WARNING, "netCI homesync: could not hand " + t.job() + "'s build to " + owner, e);
            refuse(rsp, t, owner, null);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            refuse(rsp, t, owner, null);
        }
    }

    private static void credentials(HttpRequest.Builder b, String auth, String cookie) {
        if (auth != null) {
            b.header("Authorization", auth);
        } else {
            b.header("Cookie", cookie);
        }
    }

    /** The owner's Location, as a path on this same entry point: never a replica's own address. */
    static String relative(String location) {
        URI u = URI.create(location);
        String path = encodedPath("", u.getRawPath() == null ? "/" : URLDecoder.decode(u.getRawPath(), StandardCharsets.UTF_8));
        return path.isEmpty() ? "/" : path;
    }

    static String encodedPath(String contextPath, String path) {
        String l = location(contextPath, path, null);
        return l.substring(0, l.indexOf('?'));
    }

    private static void refuse(HttpServletResponse rsp, Target t, String owner, String why) throws IOException {
        rsp.setStatus(409);
        rsp.setContentType("text/plain;charset=UTF-8");
        rsp.getWriter().println("netCI: " + t.job() + (t.starts() ? " builds" : " #" + t.number() + " runs") + " on replica " + owner
                + (why == null ? ", which could not be reached; try again in a moment" : "; " + why));
    }

    private static boolean hasBuild(Target t) {
        try (ACLContext ignored = ACL.as2(ACL.SYSTEM2)) {
            Job<?, ?> job = Jenkins.get().getItemByFullName(t.job(), Job.class);
            return job == null || job.getBuildByNumber(t.number()) != null; // no such job: Jenkins answers it
        }
    }
}
