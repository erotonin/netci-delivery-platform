package io.netci.jenkins.homesync;

import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.time.Instant;
import net.sf.json.JSONArray;
import net.sf.json.JSONObject;
import org.junit.jupiter.api.Test;

/**
 * ADR-084: whether a replica's process is gone, from what Kubernetes says. Lenient for the lab;
 * strict where only a power-off may prove it, because a machine cut off from the network keeps
 * running a pod the API already shows deleted, or reports stale.
 */
class FenceTest {
    static final long RENEWED = Instant.parse("2026-10-14T10:00:00Z").toEpochMilli();

    static JSONObject pod(String phase, String started, boolean deleting) {
        JSONObject state = started == null ? new JSONObject().element("terminated", new JSONObject().element("exitCode", 137))
                : new JSONObject().element("running", new JSONObject().element("startedAt", started));
        JSONObject meta = new JSONObject().element("name", "jenkins-0");
        if (deleting) {
            meta.element("deletionTimestamp", "2026-10-14T10:00:05Z");
        }
        return new JSONObject().element("metadata", meta).element("spec", new JSONObject().element("nodeName", "n1"))
                .element("status", new JSONObject().element("phase", phase).element("containerStatuses",
                        new JSONArray().element(new JSONObject().element("name", "jenkins").element("state", state))));
    }

    static JSONObject node(boolean ready, boolean outOfService) {
        JSONArray taints = new JSONArray();
        if (outOfService) {
            taints.add(new JSONObject().element("key", "node.kubernetes.io/out-of-service").element("value", "nodeshutdown").element("effect", "NoExecute"));
        }
        return new JSONObject().element("spec", new JSONObject().element("taints", taints)).element("status", new JSONObject().element("conditions",
                new JSONArray().element(new JSONObject().element("type", "Ready").element("status", ready ? "True" : "Unknown"))));
    }

    static final String OLD = "2026-10-14T09:00:00Z";
    static final String NEW = "2026-10-14T10:00:30Z";

    @Test
    void aPartitionedMachineIsGoneOnlyToTheLenientFence() {
        // The machine cut off: its node NotReady, its pod stale and then deleted by the node controller.
        JSONObject stale = pod("Running", OLD, false);
        JSONObject deleted = pod("Running", OLD, true);
        JSONObject notReady = node(false, false);
        assertTrue(Fence.KubernetesFence.decide(false, stale, notReady, RENEWED), "lab: a NotReady node is gone");
        assertFalse(Fence.KubernetesFence.decide(true, stale, notReady, RENEWED), "a NotReady node may still be writing");
        assertFalse(Fence.KubernetesFence.decide(true, deleted, notReady, RENEWED), "a deleted pod on a cut-off machine may still be writing");
        assertFalse(Fence.KubernetesFence.decide(true, null, null, RENEWED), "no pod and no node known: nothing is proven");
        // The supervisor powered the machine off and said so with the out-of-service taint.
        assertTrue(Fence.KubernetesFence.decide(true, stale, node(false, true), RENEWED));
        assertTrue(Fence.KubernetesFence.decide(true, deleted, node(false, true), RENEWED));
    }

    @Test
    void aKubeletThatIsReportingProvesTheOldProcessEndedInEitherMode() {
        JSONObject ready = node(true, false);
        for (boolean strict : new boolean[] {false, true}) {
            assertTrue(Fence.KubernetesFence.decide(strict, pod("Running", NEW, false), ready, RENEWED), "a new process after the renewal");
            assertTrue(Fence.KubernetesFence.decide(strict, pod("Running", null, false), ready, RENEWED), "the container ended");
            assertTrue(Fence.KubernetesFence.decide(strict, pod("Failed", OLD, false), ready, RENEWED), "the pod failed");
            assertFalse(Fence.KubernetesFence.decide(strict, pod("Running", OLD, false), ready, RENEWED), "the same process, running: never gone");
        }
    }

    @Test
    void aMissingPodIsGoneOnlyToTheLenientFence() {
        assertTrue(Fence.KubernetesFence.decide(false, null, null, RENEWED));
        assertFalse(Fence.KubernetesFence.decide(true, null, null, RENEWED));
        assertTrue(Fence.KubernetesFence.decide(false, pod("Running", OLD, true), node(true, false), RENEWED));
        assertFalse(Fence.KubernetesFence.decide(true, pod("Running", OLD, true), node(true, false), RENEWED),
                "deleting: the kubelet has not stopped it yet");
    }

    @Test
    void aRenewalInTheSecondTheNewContainerStartedIsTheOldProcesssOnceItFallsSilent() {
        // startedAt has whole seconds: the old process's last copy at 10:00:30.400, its successor's
        // container started at 10:00:30.something, reported as 10:00:30.
        long last = Instant.parse("2026-10-14T10:00:30.400Z").toEpochMilli();
        JSONObject ready = node(true, false);
        for (boolean strict : new boolean[] {false, true}) {
            assertFalse(Fence.KubernetesFence.decide(strict, pod("Running", NEW, false), ready, last, last + 1000),
                    "just written: a live writer copies every few seconds, wait");
            assertTrue(Fence.KubernetesFence.decide(strict, pod("Running", NEW, false), ready, last,
                    last + Fence.KubernetesFence.SAME_SECOND_SILENCE_MS + 1), "silent since: the old process's");
            // An old container that merely wrote nothing for a while is not gone: idle is not dead.
            assertFalse(Fence.KubernetesFence.decide(strict, pod("Running", OLD, false), ready, last, last + 600_000));
        }
        // A strict fence still believes nothing from a node that is not reporting.
        assertFalse(Fence.KubernetesFence.decide(true, pod("Running", NEW, false), node(false, false), last, last + 600_000));
    }
}
