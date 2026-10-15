package io.netci.jenkins.homesync;

import hudson.init.InitMilestone;
import hudson.init.Initializer;
import hudson.init.Terminator;
import hudson.util.PluginServletFilter;
import jakarta.servlet.Filter;
import jakarta.servlet.FilterChain;
import jakarta.servlet.ServletException;
import jakarta.servlet.ServletRequest;
import jakarta.servlet.ServletResponse;
import jakarta.servlet.http.HttpServletRequest;
import java.io.IOException;
import java.io.OutputStream;
import java.net.ServerSocket;
import java.net.Socket;
import java.nio.charset.StandardCharsets;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.logging.Level;
import java.util.logging.Logger;

/**
 * The balancer's agent check (ADR-089): {@code maint} while this replica drains, {@code ready}
 * otherwise, one line per TCP connection on {@code NETCI_DRAIN_AGENT_PORT}.
 *
 * <p>A drain used to be the health check answering 503: the balancer marked the replica down, and
 * its {@code on-marked-down shutdown-sessions} -- there for a machine that lost power, whose
 * agents' WebSockets would otherwise stay open for an hour -- closed every request in flight on it.
 * A dashboard of 22,000 jobs takes 17 s to render: the rolling upgrade to netci33 cut one, and a
 * read failed. In maintenance the balancer sends nothing new here, cookie-pinned requests included,
 * and lets what is in flight finish; the health check stays up until Jenkins is gone, and then
 * marks the replica down for what is left (the agents, which reconnect to the other one).
 *
 * <p>The pod's preStop waits for {@link #inFlight} to reach 0 (read on {@code /netci-ready/busy})
 * before Jenkins is told to stop. The answer says nothing but the word, so it is unauthenticated.
 */
public final class DrainAgent implements Filter {
    private static final Logger LOGGER = Logger.getLogger(DrainAgent.class.getName());

    /** Requests being served, not counting the drain's own polls or WebSockets (an agent's lasts hours). */
    static final AtomicInteger IN_FLIGHT = new AtomicInteger();

    private static volatile ServerSocket socket;

    /** Whether the balancer learns of a drain from this agent rather than from the health check. */
    static boolean on() {
        return HomeSync.setting("NETCI_DRAIN_AGENT_PORT", "") != null;
    }

    static int inFlight() {
        return IN_FLIGHT.get();
    }

    static String answer() {
        return ReadyForTraffic.drainFile().exists() ? "maint\n" : "ready\n";
    }

    @Initializer(after = InitMilestone.PLUGINS_STARTED)
    public static void start() throws IOException, ServletException {
        String port = HomeSync.setting("NETCI_DRAIN_AGENT_PORT", "");
        if (port == null) {
            return;
        }
        PluginServletFilter.addFilter(new DrainAgent());
        ServerSocket s = new ServerSocket(Integer.parseInt(port));
        socket = s;
        Thread t = new Thread(() -> {
            while (!s.isClosed()) {
                try (Socket c = s.accept()) {
                    c.setSoTimeout(1000);
                    OutputStream out = c.getOutputStream();
                    out.write(answer().getBytes(StandardCharsets.US_ASCII));
                    out.flush();
                } catch (IOException e) {
                    if (!s.isClosed()) {
                        LOGGER.log(Level.FINE, "netCI: the balancer's agent check connection failed", e);
                    }
                }
            }
        }, "netCI drain agent");
        t.setDaemon(true);
        t.start();
        LOGGER.info(() -> "netCI: telling the balancer of drains on port " + port);
    }

    @Terminator
    public static void stop() throws IOException {
        ServerSocket s = socket;
        socket = null;
        if (s != null) {
            s.close();
        }
    }

    @Override
    public void doFilter(ServletRequest req, ServletResponse rsp, FilterChain chain) throws IOException, ServletException {
        HttpServletRequest http = (HttpServletRequest) req;
        String path = http.getRequestURI().substring(http.getContextPath().length());
        // A WebSocket only: Java's HTTP client asks every plain request to upgrade to h2c, and
        // "any Upgrade header" left those uncounted (the test's slow page).
        if (path.startsWith("/netci-ready") || "websocket".equalsIgnoreCase(http.getHeader("Upgrade"))) {
            chain.doFilter(req, rsp);
            return;
        }
        IN_FLIGHT.incrementAndGet();
        try {
            chain.doFilter(req, rsp);
        } finally {
            IN_FLIGHT.decrementAndGet();
        }
    }
}
