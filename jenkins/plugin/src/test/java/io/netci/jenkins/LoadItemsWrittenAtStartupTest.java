package io.netci.jenkins;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertSame;
import static org.junit.jupiter.api.Assertions.assertTrue;

import hudson.init.InitMilestone;
import hudson.init.Initializer;
import hudson.model.FreeStyleProject;
import hudson.model.TopLevelItem;
import hudson.security.ACL;
import hudson.security.ACLContext;
import java.io.ByteArrayInputStream;
import java.io.File;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.util.List;
import jenkins.model.Jenkins;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.RegisterExtension;
import org.jvnet.hudson.test.junit.jupiter.JenkinsSessionExtension;

// Public: Jenkins calls declareLikeJcasc reflectively, from outside the package.
public class LoadItemsWrittenAtStartupTest {
    private static final String PROPERTY = "netci.test.declareAtStartup";
    private static final String XML = "<project><builders/><publishers/><buildWrappers/></project>";

    @RegisterExtension
    private final JenkinsSessionExtension sessions = new JenkinsSessionExtension();

    /** What Configuration as Code does with a Job DSL {@code jobs:} entry, at the same milestone. */
    @Initializer(after = InitMilestone.SYSTEM_CONFIG_LOADED, before = InitMilestone.SYSTEM_CONFIG_ADAPTED)
    public static void declareLikeJcasc() throws Exception {
        String name = System.getProperty(PROPERTY);
        if (name == null) {
            return;
        }
        try (ACLContext ignored = ACL.as2(ACL.SYSTEM2)) {
            Jenkins.get().createProjectFromXML(name, new ByteArrayInputStream(XML.getBytes(StandardCharsets.UTF_8)));
        }
    }

    @Test
    void aJobDeclaredDuringStartUpIsThereWhenJenkinsIsUp() throws Throwable {
        sessions.then(r -> r.createFreeStyleProject("existing"));
        System.setProperty(PROPERTY, "declared");
        try {
            sessions.then(r -> {
                assertTrue(new File(r.jenkins.getRootDir(), "jobs/declared/config.xml").isFile(), "the declaration ran");
                assertNotNull(r.jenkins.getItem("declared"),
                        "a job written during start-up is on disk but not in Jenkins: Jenkins dropped it as obsolete");
                assertNotNull(r.jenkins.getItem("existing"));
            });
        } finally {
            System.clearProperty(PROPERTY);
        }
    }

    @Test
    void onlyWhatIsMissingIsLoadedAndOnlyOnce() throws Throwable {
        sessions.then(r -> {
            FreeStyleProject existing = r.createFreeStyleProject("existing");
            File dir = new File(r.jenkins.getRootDir(), "jobs/on-disk-only");
            Files.createDirectories(dir.toPath());
            Files.writeString(new File(dir, "config.xml").toPath(), XML);
            Files.createDirectories(new File(r.jenkins.getRootDir(), "jobs/not-a-job").toPath());

            assertEquals(List.of("on-disk-only"), LoadItemsWrittenAtStartup.load(r.jenkins));
            TopLevelItem loaded = r.jenkins.getItem("on-disk-only");
            assertTrue(loaded instanceof FreeStyleProject);
            assertSame(existing, r.jenkins.getItem("existing"), "an item already loaded was replaced");
            assertEquals(List.of(), LoadItemsWrittenAtStartup.load(r.jenkins));
            assertSame(loaded, r.jenkins.getItem("on-disk-only"));
        });
    }
}
