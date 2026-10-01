package io.netci.jenkins;

import hudson.init.InitMilestone;
import hudson.init.Initializer;
import hudson.model.Items;
import hudson.model.TopLevelItem;
import hudson.security.ACL;
import hudson.security.ACLContext;
import java.io.File;
import java.io.IOException;
import java.util.ArrayList;
import java.util.List;
import java.util.logging.Level;
import java.util.logging.Logger;
import jenkins.model.Jenkins;

/**
 * Loads the top-level items that are on disk but not in Jenkins once its jobs are loaded.
 *
 * <p>Jenkins lists {@code JENKINS_HOME/jobs} when it builds its start-up tasks, before
 * Configuration as Code runs. A job that Job DSL then creates from JCasC is put in memory and
 * written to disk, and the task "Cleaning up obsolete items deleted from the disk" removes it
 * from memory because it was not in that listing. Its {@code config.xml} stays. So every job newly
 * declared in JCasC was missing after the start that declared it, until someone reloaded -- and
 * a run netci-queue dispatched to it meanwhile was refused as a job that does not exist. The lab
 * hit this each time a job was added to a cell.
 *
 * <p>The disk is what Jenkins itself treats as the truth here, so what is on it is loaded.
 */
public final class LoadItemsWrittenAtStartup {
    private static final Logger LOGGER = Logger.getLogger(LoadItemsWrittenAtStartup.class.getName());

    private LoadItemsWrittenAtStartup() {}

    @Initializer(after = InitMilestone.JOB_LOADED, before = InitMilestone.JOB_CONFIG_ADAPTED)
    public static void loadUnlisted() {
        List<String> loaded = load(Jenkins.get());
        if (!loaded.isEmpty()) {
            LOGGER.info(() -> "netCI: loaded " + loaded + ", written during start-up after Jenkins had listed its jobs "
                    + "(a job new in JCasC is otherwise dropped until a reload)");
        }
    }

    static List<String> load(Jenkins jenkins) {
        List<String> loaded = new ArrayList<>();
        File[] dirs = new File(jenkins.getRootDir(), "jobs").listFiles(File::isDirectory);
        if (dirs == null) {
            return loaded;
        }
        try (ACLContext ignored = ACL.as2(ACL.SYSTEM2)) {
            for (File dir : dirs) {
                if (!Items.getConfigFile(dir).exists() || jenkins.getItem(dir.getName()) != null) {
                    continue;
                }
                try {
                    TopLevelItem item = (TopLevelItem) Items.load(jenkins, dir);
                    jenkins.putItem(item);
                    loaded.add(item.getName());
                } catch (IOException | RuntimeException e) {
                    LOGGER.log(Level.WARNING, "netCI: cannot load " + dir + ", found on disk but not loaded by Jenkins", e);
                } catch (InterruptedException e) {
                    Thread.currentThread().interrupt();
                    return loaded;
                }
            }
        }
        return loaded;
    }
}
