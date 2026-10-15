package io.netci.jenkins.homesync;

import hudson.Extension;
import hudson.FilePath;
import hudson.model.Run;
import hudson.model.listeners.RunListener;
import java.io.ByteArrayOutputStream;
import java.io.OutputStream;
import hudson.XmlFile;
import hudson.model.AbstractItem;
import hudson.model.Item;
import hudson.model.Saveable;
import hudson.model.User;
import hudson.model.listeners.ItemListener;
import hudson.model.listeners.SaveableListener;
import java.io.IOException;
import java.nio.file.Files;
import java.util.logging.Level;
import java.util.logging.Logger;

/** Publishes what this replica saves or deletes, unless it is applying another replica's change. */
public final class PublishChanges {
    /** A finished build, and a deleted one (ADR-078). */
    @Extension
    public static final class OnBuild extends RunListener<Run<?, ?>> {
        @Override
        public void onFinalized(Run<?, ?> run) {
            HomeSync sync = HomeSync.get();
            if (sync == null || !sync.ready()) {
                return;
            }
            byte[] archive;
            try {
                archive = RunningBuilds.archive(run.getRootDir());
            } catch (IOException e) {
                LOGGER.log(Level.SEVERE, "netCI homesync: cannot archive " + run + " to publish it; the other replicas will not have it", e);
                return;
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
                return;
            }
            sync.publish(HomeSync.buildPath(run), archive, 0);
        }

        @Override
        public void onDeleted(Run<?, ?> run) {
            HomeSync sync = HomeSync.get();
            if (sync == null || !sync.ready() || HomeSync.APPLYING.get()) {
                return;
            }
            sync.publishDelete(HomeSync.buildPath(run), 0);
        }
    }

    private static final Logger LOGGER = Logger.getLogger(PublishChanges.class.getName());

    private PublishChanges() {}

    /** An item's config.xml was written. */
    @Extension
    public static final class OnSave extends SaveableListener {
        @Override
        public void onChange(Saveable o, XmlFile file) {
            HomeSync sync = HomeSync.get();
            if (sync == null || !sync.ready() || HomeSync.APPLYING.get()) {
                return;
            }
            if (o instanceof User user && file.getFile().getName().equals("config.xml")) {
                String path = HomeSync.userPath(user.getId());
                try {
                    sync.publish(path, Files.readAllBytes(file.getFile().toPath()), EditOrder.tokenFor(path));
                } catch (IOException e) {
                    LOGGER.log(Level.SEVERE, "netCI homesync: cannot read the saved user " + user.getId() + " to publish it", e);
                }
                return;
            }
            if (!(o instanceof AbstractItem item)) {
                return;
            }
            // Only the item's own configuration: builds and other files an item saves are not it.
            if (!file.getFile().equals(item.getConfigFile().getFile()) || EditOrder.pendingMove(item.getFullName())) {
                return;
            }
            try {
                sync.publish(item.getFullName(), Files.readAllBytes(file.getFile().toPath()),
                        EditOrder.tokenFor(item.getFullName()));
            } catch (IOException e) {
                LOGGER.log(Level.SEVERE, "netCI homesync: cannot read the saved " + file + " to publish it", e);
            }
        }
    }

    /** Items created here, and deletions. */
    @Extension
    public static final class OnCreateOrDelete extends ItemListener {
        /**
         * A job created from XML (POST createItem with a config.xml, a copy) is written to disk and
         * loaded without a save, so the save listener never hears of it: the first lab run of
         * phase 3 created ten jobs on one replica and none reached the other.
         */
        @Override
        public void onCreated(Item created) {
            HomeSync sync = HomeSync.get();
            if (sync == null || !sync.ready() || HomeSync.APPLYING.get() || !(created instanceof AbstractItem item)
                    || EditOrder.pendingMove(item.getFullName())) {
                return;
            }
            try {
                sync.publish(item.getFullName(), Files.readAllBytes(item.getConfigFile().getFile().toPath()),
                        EditOrder.tokenFor(item.getFullName()));
            } catch (IOException e) {
                LOGGER.log(Level.SEVERE, "netCI homesync: cannot read the created " + item.getFullName() + " to publish it", e);
            }
        }

        @Override
        public void onDeleted(Item item) {
            HomeSync sync = HomeSync.get();
            if (sync == null || !sync.ready() || HomeSync.APPLYING.get() || EditOrder.pendingMove(item.getFullName())) {
                return;
            }
            sync.publishDelete(item.getFullName(), EditOrder.tokenFor(item.getFullName()));
        }

        /**
         * A rename or a move, made here (ADR-087). Jenkins moves the directory and then says so,
         * without a save: before this, the other replicas got the new name as a new job with no
         * builds, and kept the old one.
         */
        @Override
        public void onLocationChanged(Item item, String oldFullName, String newFullName) {
            HomeSync sync = HomeSync.get();
            if (sync == null || HomeSync.APPLYING.get() || HomeSync.childOfMove(oldFullName, newFullName)) {
                return;
            }
            if (!sync.ready()) {
                LOGGER.severe(() -> "netCI homesync: " + oldFullName + " was moved to " + newFullName + " before this replica caught up;"
                        + " the other replicas still have it under its old name");
                return;
            }
            sync.publishMove(oldFullName, newFullName, EditOrder.tokenFor(oldFullName), EditOrder.tokenFor(newFullName));
        }
    }
}
