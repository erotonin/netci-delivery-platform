package io.netci.jenkins.homesync;

import hudson.model.Computer;
import hudson.slaves.OfflineCause;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Duration;
import java.util.Map;
import java.util.logging.Logger;
import jenkins.model.Jenkins;

/**
 * An idle inbound agent connected here, whose home replica is back, is sent home (ADR-079).
 *
 * <p>Builds of a job can use only the agents connected to the job's owner. When a replica dies, its
 * agents reconnect through the VIP to another one -- which is what lets that one adopt the builds --
 * but they stayed there after the replica came back: in the lab both agents ended on r1 while r0
 * owned the job, and r0's build waited with "all nodes of label 'linux' are offline". An agent is
 * disconnected only when it is idle and its home answers; it reconnects by itself, and the balancer
 * sends it home by its name. {@code NETCI_AGENT_HOMES} maps agents to replicas
 * ({@code agent-0=jenkins-0,...}), the same mapping the balancer routes by.
 */
final class AgentsGoHome {
    private static final Logger LOGGER = Logger.getLogger(AgentsGoHome.class.getName());
    private static final HttpClient HTTP = HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(2)).build();

    private AgentsGoHome() {}

    static void pass() {
        Map<String, String> homes = OwnerRouting.pairs(HomeSync.setting("NETCI_AGENT_HOMES", ""));
        Map<String, String> urls = OwnerRouting.pairs(HomeSync.setting("NETCI_REPLICA_URLS", ""));
        String me = OwnerRouting.me();
        if (homes.isEmpty() || me.isEmpty()) {
            return;
        }
        for (Computer c : Jenkins.get().getComputers()) {
            String home = homes.get(c.getName());
            if (home == null || home.equals(me) || c.isOffline() || !c.isIdle() || awaited(c.getName()) || !answers(urls.get(home))) {
                continue;
            }
            LOGGER.info(() -> "netCI homesync: " + c.getName() + " is idle here and its home " + home + " is back; sending it home");
            c.disconnect(new SentHome(home));
        }
    }

    /** Why the agent went offline here, as the computer's page says it. */
    static final class SentHome extends OfflineCause {
        private final String home;

        SentHome(String home) {
            this.home = home;
        }

        @Override
        public String toString() {
            return "netCI: idle, sent home to replica " + home;
        }
    }

    /**
     * Something in the queue waits for this very agent: a build adopted here re-queues its node block
     * for the agent it was on, and the agent's executors look idle until it gets one. The lab's first
     * machine loss sent the agent home in that gap, away from the build adopted for it.
     */
    static boolean awaited(String agent) {
        for (hudson.model.Queue.Item item : hudson.model.Queue.getInstance().getItems()) {
            hudson.model.Label label = item.getAssignedLabel();
            if (label != null && agent.equals(label.getName())) {
                return true;
            }
        }
        return false;
    }

    private static boolean answers(String url) {
        if (url == null) {
            return false;
        }
        try {
            HttpResponse<Void> r = HTTP.send(HttpRequest.newBuilder(URI.create(url + "/login")).timeout(Duration.ofSeconds(3)).GET().build(),
                    HttpResponse.BodyHandlers.discarding());
            return r.statusCode() == 200;
        } catch (java.io.IOException e) {
            return false;
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            return false;
        }
    }
}
