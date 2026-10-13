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
import java.io.IOException;
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

    OwnerRouting(String me, String cookieName, Map<String, String> cookies) {
        this.me = me;
        this.cookieName = cookieName;
        this.cookies = cookies;
    }

    @Initializer(after = InitMilestone.PLUGINS_STARTED)
    public static void register() throws ServletException {
        String me = HomeSync.setting("NETCI_REPLICA_NAME", HomeSync.setting("POD_NAME", ""));
        String map = HomeSync.setting("NETCI_REPLICA_COOKIES", "");
        if (HomeSync.setting("NETCI_HOMESYNC_URL", "") == null || me == null || map == null) {
            return;
        }
        Map<String, String> cookies = new HashMap<>();
        for (String pair : map.split(",")) {
            String[] kv = pair.trim().split("=", 2);
            if (kv.length == 2) {
                cookies.put(kv[0].trim(), kv[1].trim());
            }
        }
        PluginServletFilter.addFilter(new OwnerRouting(me, HomeSync.setting("NETCI_BALANCER_COOKIE", "NETCI_REPLICA"), cookies));
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

    private static boolean hasBuild(Target t) {
        try (ACLContext ignored = ACL.as2(ACL.SYSTEM2)) {
            Job<?, ?> job = Jenkins.get().getItemByFullName(t.job(), Job.class);
            return job == null || job.getBuildByNumber(t.number()) != null; // no such job: Jenkins answers it
        }
    }
}
