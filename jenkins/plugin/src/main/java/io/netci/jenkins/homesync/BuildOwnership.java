package io.netci.jenkins.homesync;

import hudson.Extension;
import hudson.model.Action;
import hudson.model.Job;
import hudson.model.Queue;
import hudson.model.Run;
import hudson.model.listeners.RunListener;
import hudson.model.queue.CauseOfBlockage;
import hudson.model.queue.QueueTaskDispatcher;
import java.io.IOException;
import java.time.Duration;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;
import java.util.logging.Level;
import java.util.logging.Logger;

/**
 * One owner per job, numbers reserved before a build takes them (ADR-078).
 *
 * <p>Only the replica holding a job's lease schedules it: on any other replica the scheduling is
 * declined (a cron trigger fires on every replica, and only one of them may act on it). When the
 * queue offers an item to an executor -- the last moment before Jenkins gives the run its number --
 * the owner reserves that number in netci-homesync under its lease epoch; while the reservation is
 * not consumed by a started run, other items of the job wait. A replica that lost the lease cannot
 * reserve, so it cannot start a build.
 *
 * <p>Off with homesync. With homesync on but unreachable, nothing is scheduled: two replicas
 * building one job is the failure this exists to prevent, and a build that waits is not lost.
 */
public final class BuildOwnership {
    private static final Logger LOGGER = Logger.getLogger(BuildOwnership.class.getName());
    static final Duration LEASE = Duration.ofSeconds(15);
    /** A lease with less than this left is renewed before it is relied on. */
    static final Duration MARGIN = Duration.ofSeconds(5);
    private static final Duration PENDING = Duration.ofSeconds(60);

    private record Lease(long epoch, long validUntilMillis) {}

    private record Pending(long itemId, int number, long sinceMillis) {}

    private static final Map<String, Lease> LEASES = new ConcurrentHashMap<>();
    private static final Map<String, Pending> PENDING_BY_JOB = new ConcurrentHashMap<>();

    private BuildOwnership() {}

    static void forget() {
        LEASES.clear();
        PENDING_BY_JOB.clear();
    }

    /** This replica holds a lease on the job valid for at least MARGIN, claiming or renewing it if needed. */
    static boolean own(HomeSync sync, Job<?, ?> job) {
        Lease held = LEASES.get(job.getFullName());
        if (held != null && held.validUntilMillis() - System.currentTimeMillis() > MARGIN.toMillis()) {
            return true;
        }
        try {
            long asked = System.currentTimeMillis();
            HomeSyncClient.JobLease lease = sync.client.claimJob(job.getFullName(), LEASE);
            // Valid from when it was asked for, not when the answer came: the conservative end.
            LEASES.put(job.getFullName(), new Lease(lease.epoch(), asked + lease.expiresInMs()));
            if (held == null || held.epoch() != lease.epoch()) {
                // Newly owned: continue above every number anyone reserved for this job.
                if (lease.maxReserved() >= job.getNextBuildNumber()) {
                    job.updateNextBuildNumber(lease.maxReserved() + 1);
                }
            }
            return true;
        } catch (HomeSyncClient.Refused r) {
            LEASES.remove(job.getFullName());
            return false;
        } catch (IOException e) {
            LOGGER.log(Level.WARNING, "netCI homesync: cannot reach the store to hold " + job.getFullName() + "'s lease", e);
            return false;
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            return false;
        }
    }

    /** Only the owner schedules a job's builds. */
    @Extension
    public static final class OnlyTheOwnerSchedules extends Queue.QueueDecisionHandler {
        @Override
        public boolean shouldSchedule(Queue.Task task, List<Action> actions) {
            HomeSync sync = HomeSync.get();
            if (sync == null || !(task instanceof Job<?, ?> job)) {
                return true;
            }
            if (!sync.ready() || !own(sync, job)) {
                LOGGER.info(() -> "netCI homesync: not scheduling " + job.getFullName() + " here: another replica owns it, "
                        + "or the store cannot be reached");
                return false;
            }
            return true;
        }
    }

    /** The number is reserved before the run takes it. */
    @Extension
    public static final class ReserveTheNumber extends QueueTaskDispatcher {
        /**
         * canRun, not canTake: a Pipeline run is a flyweight task, started without the queue asking
         * any dispatcher's canTake -- the unit test showed no number reserved at all through it.
         * canRun is asked of every item before it may become buildable, flyweights included, and
         * still before the executor gives the run its number.
         */
        @Override
        public CauseOfBlockage canRun(Queue.Item item) {
            HomeSync sync = HomeSync.get();
            if (sync == null || !(item.task instanceof Job<?, ?> job)) {
                return null;
            }
            String name = job.getFullName();
            Pending pending = PENDING_BY_JOB.get(name);
            if (pending != null) {
                if (pending.itemId() == item.getId()) {
                    return null; // offered again to another executor: the same reservation
                }
                if (System.currentTimeMillis() - pending.sinceMillis() < PENDING.toMillis()) {
                    return blockage("netCI: waiting for " + name + " #" + pending.number() + " to start before reserving the next number");
                }
                PENDING_BY_JOB.remove(name, pending); // reserved for an item that never started
            }
            if (!own(sync, job)) {
                return blockage("netCI: this replica does not hold " + name + "'s lease; the build waits");
            }
            Lease lease = LEASES.get(name);
            int number = job.getNextBuildNumber();
            try {
                sync.client.reserve(name, number, lease.epoch());
                PENDING_BY_JOB.put(name, new Pending(item.getId(), number, System.currentTimeMillis()));
                return null;
            } catch (HomeSyncClient.Refused r) {
                if ("NUMBER_TAKEN".equals(r.code)) {
                    try {
                        job.updateNextBuildNumber((int) r.current + 1);
                    } catch (IOException e) {
                        LOGGER.log(Level.WARNING, "netCI homesync: cannot move " + name + "'s next number past " + r.current, e);
                    }
                    return blockage("netCI: " + name + " #" + number + " was taken; renumbering");
                }
                LEASES.remove(name);
                return blockage("netCI: " + name + "'s lease was lost; the build waits");
            } catch (IOException e) {
                return blockage("netCI: the store cannot be reached to reserve " + name + " #" + number + "; the build waits");
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
                return blockage("netCI: interrupted while reserving " + name + " #" + number);
            }
        }

        private static CauseOfBlockage blockage(String message) {
            return new CauseOfBlockage() {
                @Override
                public String getShortDescription() {
                    return message;
                }
            };
        }
    }

    /** A started run consumes its job's reservation. */
    @Extension
    public static final class Consume extends RunListener<Run<?, ?>> {
        @Override
        public void onInitialize(Run<?, ?> run) {
            String name = run.getParent().getFullName();
            Pending pending = PENDING_BY_JOB.remove(name);
            if (pending != null && pending.number() != run.getNumber()) {
                LOGGER.severe(() -> "netCI homesync: " + name + " started as #" + run.getNumber() + " but #" + pending.number()
                        + " was reserved: the numbering is not what the store holds");
            }
        }
    }

    /** Keeps the leases this replica holds alive while it is. */
    static void renewAll(HomeSync sync) {
        for (Map.Entry<String, Lease> e : LEASES.entrySet()) {
            try {
                HomeSyncClient.JobLease lease = sync.client.claimJob(e.getKey(), LEASE);
                LEASES.put(e.getKey(), new Lease(lease.epoch(), System.currentTimeMillis() + lease.expiresInMs() - 500));
            } catch (HomeSyncClient.Refused r) {
                LEASES.remove(e.getKey());
                LOGGER.warning(() -> "netCI homesync: lost the lease of " + e.getKey() + " to " + r.holder);
            } catch (IOException ex) {
                LOGGER.log(Level.FINE, "netCI homesync: could not renew " + e.getKey(), ex);
            } catch (InterruptedException ex) {
                Thread.currentThread().interrupt();
                return;
            }
        }
    }
}
