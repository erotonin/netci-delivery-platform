package io.netci.jenkins.homesync;

import java.io.ByteArrayInputStream;
import java.io.IOException;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.security.KeyStore;
import java.security.cert.CertificateFactory;
import java.time.Duration;
import java.time.Instant;
import java.util.logging.Level;
import java.util.logging.Logger;
import javax.net.ssl.SSLContext;
import javax.net.ssl.TrustManagerFactory;
import net.sf.json.JSONArray;
import net.sf.json.JSONObject;

/**
 * What says a replica is gone before its running builds are adopted (ADR-079). Without one,
 * nothing is adopted: continuing a build whose owner is still running would run it twice.
 */
abstract class Fence {
    private static final Logger LOGGER = Logger.getLogger(Fence.class.getName());

    /** The process that last renewed a lease at {@code lastRenewed} (epoch ms) is not running any more. */
    abstract boolean isGone(String replica, long lastRenewed);

    private static volatile Fence kubernetes;

    static Fence configured() {
        String mode = HomeSync.setting("NETCI_ADOPT_FENCE", "");
        if (!"kubernetes".equals(mode) && !"kubernetes-power".equals(mode)) {
            return null;
        }
        if (kubernetes == null) {
            try {
                kubernetes = new KubernetesFence(Path.of(HomeSync.setting("NETCI_KUBERNETES_ACCOUNT",
                        "/var/run/secrets/kubernetes.io/serviceaccount")), HomeSync.setting("NETCI_KUBERNETES_API", "https://kubernetes.default.svc"),
                        "kubernetes-power".equals(mode));
            } catch (IOException | java.security.GeneralSecurityException e) {
                LOGGER.log(Level.SEVERE, "netCI homesync: the Kubernetes fence cannot be set up; nothing is adopted", e);
                return null;
            }
        }
        return kubernetes;
    }

    /**
     * The replica's pod (named as the replica) is gone if Kubernetes says: no such pod; being
     * deleted; not running; its container not running, or started after the lease was last renewed
     * (a new process: the old one is dead); or its machine not Ready. A pod that is merely not ready
     * -- slow, overloaded -- is NOT gone: adopting from a live replica is the failure this prevents.
     * What the API cannot prove is a machine that is partitioned yet alive; a site fences that by
     * power (the supervisor, ADR-060/069).
     */
    static final class KubernetesFence extends Fence {
        /**
         * The decision, from what the API says of the replica's pod (null: no such pod) and of its
         * node (null: unknown).
         *
         * <p>Lenient (the lab): gone when the pod is missing or being deleted, not running, its
         * container not running or newer than the last renewal, or its node not Ready.
         *
         * <p>Strict (production, {@code NETCI_ADOPT_FENCE=kubernetes-power}): only what proves the old
         * process cannot write. Its node carries the out-of-service taint, which the supervisor puts
         * on only once the machine's power controller reports it off (ADR-060, ADR-084). Or the
         * kubelet of a node that is Ready reports the old process ended (container not running, or a
         * new one started after the renewal). A missing or deleted pod, or a NotReady node, is not
         * enough: a machine cut off from the network keeps running its pod, which the API then shows
         * deleted or stale.
         */
        /**
         * How long the writer of a renewal made in the very second the pod's container started must
         * have been silent: two of a running build's copies (RunningBuilds.EVERY_SECONDS) and a
         * second's margin. Kubernetes reports startedAt in whole seconds. A force-deleted replica's
         * old process lived on for a second or two and wrote one last copy; when that copy fell in the
         * second its successor's container started, "started after the renewal" could never be true,
         * and no survivor adopted the build -- it waited 50-60 s for the restarted replica to boot and
         * take it back itself (the lab, 2 of 4 pod kills, 2026-10-15).
         */
        static final long SAME_SECOND_SILENCE_MS = (2 * RunningBuilds.EVERY_SECONDS + 1) * 1000;

        static boolean decide(boolean strict, JSONObject pod, JSONObject node, long lastRenewed) {
            return decide(strict, pod, node, lastRenewed, System.currentTimeMillis());
        }

        static boolean decide(boolean strict, JSONObject pod, JSONObject node, long lastRenewed, long now) {
            boolean ready = false;
            boolean fenced = false;
            if (node != null) {
                JSONArray conditions = node.getJSONObject("status").optJSONArray("conditions");
                for (int i = 0; conditions != null && i < conditions.size(); i++) {
                    JSONObject c = conditions.getJSONObject(i);
                    if ("Ready".equals(c.optString("type"))) {
                        ready = "True".equals(c.optString("status"));
                    }
                }
                JSONArray taints = node.getJSONObject("spec").optJSONArray("taints");
                for (int i = 0; taints != null && i < taints.size(); i++) {
                    if ("node.kubernetes.io/out-of-service".equals(taints.getJSONObject(i).optString("key"))) {
                        fenced = true;
                    }
                }
            }
            if (strict && fenced) {
                return true;
            }
            if (pod == null || pod.getJSONObject("metadata").has("deletionTimestamp")) {
                return !strict;
            }
            if (strict && !ready) {
                return false; // the kubelet is not reporting: what the API shows of the pod is stale
            }
            JSONObject status = pod.getJSONObject("status");
            if (!"Running".equals(status.optString("phase"))) {
                return true;
            }
            JSONArray containers = status.optJSONArray("containerStatuses");
            for (int i = 0; containers != null && i < containers.size(); i++) {
                JSONObject c = containers.getJSONObject(i);
                if (!"jenkins".equals(c.optString("name"))) {
                    continue;
                }
                JSONObject running = c.getJSONObject("state").optJSONObject("running");
                if (running == null || running.isNullObject()) {
                    return true;
                }
                long started = Instant.parse(running.getString("startedAt")).toEpochMilli();
                if (started > lastRenewed) {
                    return true; // started after the lease's last renewal: not the process that held it
                }
                // The same second: the order is not in the API. A container that has run for less
                // than a second has written nothing (a JVM and Jenkins take seconds to start), so
                // the renewal is an older process's -- gone once it has been silent for longer than
                // a live one ever is. The clocks compared are the store's and this replica's.
                if (started == lastRenewed / 1000 * 1000 && now - lastRenewed > SAME_SECOND_SILENCE_MS) {
                    return true;
                }
            }
            return !strict && node != null && !ready;
        }

        private final Path account;
        private final String api;
        private final SSLContext tls;
        private final boolean strict;

        KubernetesFence(Path account, String api, boolean strict) throws IOException, java.security.GeneralSecurityException {
            this.account = account;
            this.api = api;
            this.strict = strict;
            KeyStore trust = KeyStore.getInstance(KeyStore.getDefaultType());
            trust.load(null, null);
            byte[] ca = Files.readAllBytes(account.resolve("ca.crt"));
            int i = 0;
            for (java.security.cert.Certificate c : CertificateFactory.getInstance("X.509").generateCertificates(new ByteArrayInputStream(ca))) {
                trust.setCertificateEntry("k8s-" + i++, c);
            }
            TrustManagerFactory tmf = TrustManagerFactory.getInstance(TrustManagerFactory.getDefaultAlgorithm());
            tmf.init(trust);
            this.tls = SSLContext.getInstance("TLS");
            this.tls.init(null, tmf.getTrustManagers(), null);
        }

        /**
         * A new connection each attempt, three attempts. The API's Service spreads connections over
         * every control-plane node, and a pooled connection that landed on the machine that died kept
         * landing there: the fence answered "cannot tell" for four minutes (the lab's machine loss).
         */
        private HttpResponse<String> get(String path) throws IOException, InterruptedException {
            String token = Files.readString(account.resolve("token"), StandardCharsets.UTF_8).trim();
            IOException last = null;
            for (int attempt = 0; attempt < 3; attempt++) {
                try (HttpClient http = HttpClient.newBuilder().sslContext(tls).connectTimeout(Duration.ofSeconds(2)).build()) {
                    return http.send(HttpRequest.newBuilder(URI.create(api + path)).timeout(Duration.ofSeconds(3))
                            .header("Authorization", "Bearer " + token).GET().build(), HttpResponse.BodyHandlers.ofString());
                } catch (IOException e) {
                    last = e;
                }
            }
            throw last;
        }

        @Override
        boolean isGone(String replica, long lastRenewed) {
            if (replica == null || replica.isEmpty()) {
                return true; // nobody ever held it
            }
            try {
                String ns = Files.readString(account.resolve("namespace"), StandardCharsets.UTF_8).trim();
                HttpResponse<String> r = get("/api/v1/namespaces/" + ns + "/pods/" + replica);
                if (r.statusCode() != 200 && r.statusCode() != 404) {
                    return false; // cannot tell: not gone
                }
                JSONObject pod = r.statusCode() == 404 ? null : JSONObject.fromObject(r.body());
                JSONObject node = null;
                String name = pod == null ? "" : pod.getJSONObject("spec").optString("nodeName");
                if (!name.isEmpty()) {
                    HttpResponse<String> n = get("/api/v1/nodes/" + name);
                    if (n.statusCode() == 200) {
                        node = JSONObject.fromObject(n.body());
                    }
                }
                return decide(strict, pod, node, lastRenewed);
            } catch (IOException | RuntimeException e) {
                LOGGER.log(Level.WARNING, "netCI homesync: the fence could not tell whether " + replica + " is gone", e);
                return false;
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
                return false;
            }
        }
    }
}
