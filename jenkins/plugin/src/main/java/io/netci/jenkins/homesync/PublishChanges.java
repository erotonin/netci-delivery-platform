package io.netci.jenkins.homesync;

import hudson.Extension;
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
            if (!file.getFile().equals(item.getConfigFile().getFile())) {
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
            if (sync == null || !sync.ready() || HomeSync.APPLYING.get() || !(created instanceof AbstractItem item)) {
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
            if (sync == null || !sync.ready() || HomeSync.APPLYING.get()) {
                return;
            }
            sync.publishDelete(item.getFullName(), EditOrder.tokenFor(item.getFullName()));
        }
    }
}
