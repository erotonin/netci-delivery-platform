package io.netci.jenkins.homesync;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertTrue;

import org.junit.jupiter.api.Test;

class EditOrderTest {
    @Test
    void anEditNamesTheItemItChanges() {
        assertEquals("app", EditOrder.editedItem("POST", "/job/app/configSubmit", null));
        assertEquals("team/app", EditOrder.editedItem("POST", "/job/team/job/app/config.xml", null));
        assertEquals("team/new", EditOrder.editedItem("POST", "/job/team/createItem", "new"));
        assertEquals("top", EditOrder.editedItem("POST", "/createItem", "top"));
        assertEquals("with space", EditOrder.editedItem("POST", "/job/with%20space/disable", null));
        assertEquals("@users/alice", EditOrder.editedItem("POST", "/user/alice/configSubmit", null));
        assertEquals("@users/alice", EditOrder.editedItem("POST",
                "/user/alice/descriptorByName/jenkins.security.ApiTokenProperty/generateNewToken", null));
    }

    @Test
    void theRoutedLocationIsReEncodedSoNothingRawReachesTheHeader() {
        assertEquals("/jenkins/job/a%20b/buildWithParameters?X=1+2&netci-routed=1",
                OwnerRouting.location("/jenkins", "/job/a%20b/buildWithParameters", "X=1%202"));
        String injected = OwnerRouting.location("", "/job/app/build", "a=%0d%0aSet-Cookie:%20x=1");
        assertEquals(-1, injected.indexOf('\r'));
        assertEquals(-1, injected.indexOf('\n'));
        assertEquals("/job/app/7/?netci-routed=1", OwnerRouting.location("", "/job/app/7/", OwnerRouting.ROUTED + "=1"));
    }

    @Test
    void aRunningPipelineIsTheOneWhoseBuildXmlSaysNotCompleted() {
        assertTrue(RunningBuilds.unfinished("<flow-build><result>SUCCESS</result><completed>false</completed></flow-build>"));
        assertTrue(!RunningBuilds.unfinished("<flow-build><result>SUCCESS</result><completed>true</completed></flow-build>"));
        assertTrue(RunningBuilds.unfinished("<build><number>3</number></build>"));
        assertTrue(!RunningBuilds.unfinished("<build><result>FAILURE</result></build>"));
    }

    @Test
    void theAgentABuildIsOnIsTheLastNodeItEnteredAndHasNotLeft() throws java.io.IOException {
        assertEquals("agent-0", RunningBuilds.agentInLog("[Pipeline] node\nRunning on agent-0 in /w\n[Pipeline] {\n[Pipeline] sh\ntick 3\n"));
        assertEquals("b", RunningBuilds.agentInLog("Running on a in /w\n[Pipeline] // node\nRunning on b in /w\ntick\n"));
        assertNull(RunningBuilds.agentInLog("Running on a in /w\n[Pipeline] // node\n[Pipeline] echo\n"));
        assertNull(RunningBuilds.agentInLog("[Pipeline] sleep\n"));
        // As the log file has it: the name is a console note (a hyperlink) -- the first lab run's miss.
        String noted = new hudson.console.HyperlinkNote("/computer/agent-0/", 7).encode() + "agent-0";
        assertEquals("agent-0", RunningBuilds.agentInLog("Running on " + noted + " in /w\n[Pipeline] {\n"));
    }

    @Test
    void readsBuildsAndOtherPagesAreNotEdits() {
        assertNull(EditOrder.editedItem("GET", "/job/app/config.xml", null));
        assertNull(EditOrder.editedItem("POST", "/job/app/build", null));
        assertNull(EditOrder.editedItem("POST", "/job/app/12/stop", null));
        assertNull(EditOrder.editedItem("POST", "/job/app/job/x", null));
        assertNull(EditOrder.editedItem("POST", "/configSubmit", null)); // global config: JCasC's, not an item
        assertNull(EditOrder.editedItem("POST", "/createItem", ""));
        assertNull(EditOrder.editedItem("GET", "/user/alice/configure", null));
        assertNull(EditOrder.editedItem("POST", "/user/alice", null));
    }

    @Test
    void aRenameOrAMoveNamesBothItsNames() {
        assertEquals(new EditOrder.Move("app", "svc"), EditOrder.moved("POST", "/job/app/confirmRename", "svc", null));
        assertEquals(new EditOrder.Move("team/app", "team/svc"), EditOrder.moved("POST", "/job/team/job/app/confirmRename", " svc ", null));
        assertEquals(new EditOrder.Move("app", "team/app"), EditOrder.moved("POST", "/job/app/move/move", null, "/team"));
        assertEquals(new EditOrder.Move("team/app", "app"), EditOrder.moved("POST", "/job/team/job/app/move/move", null, "/"));
        assertEquals(new EditOrder.Move("a b", "x/a b"), EditOrder.moved("POST", "/job/a%20b/move/move", null, "/x"));
        assertNull(EditOrder.moved("GET", "/job/app/confirmRename", "svc", null));
        assertNull(EditOrder.moved("POST", "/job/app/confirmRename", "", null));
        assertNull(EditOrder.moved("POST", "/job/app/move/move", null, "team"));
        assertNull(EditOrder.moved("POST", "/job/app/move", null, "/team"));
        assertNull(EditOrder.moved("POST", "/confirmRename", "x", null));
        assertTrue(new EditOrder.Move("a", "b").covers("a/x"));
        assertTrue(new EditOrder.Move("a", "b").covers("b"));
        assertTrue(!new EditOrder.Move("a", "b").covers("ab"));
    }

    @Test
    void aViewsEditNamesTheViewAndAJobsPageThroughAViewNamesTheJob() {
        assertEquals("@views/team", EditOrder.editedView("POST", "/createView", "team"));
        assertEquals("@views/team", EditOrder.editedView("POST", "/view/team/configSubmit", null));
        assertEquals("@views/team", EditOrder.editedView("POST", "/view/team/addJobToView", null));
        assertEquals("@views/a b", EditOrder.editedView("POST", "/view/a%20b/doDelete", null));
        assertEquals("@views/outer", EditOrder.editedView("POST", "/view/outer/view/inner/configSubmit", null));
        assertNull(EditOrder.editedView("GET", "/view/team/configSubmit", null));
        assertNull(EditOrder.editedView("POST", "/view/team/job/app/configSubmit", null));
        assertNull(EditOrder.editedView("POST", "/view/team/build", null));
        assertNull(EditOrder.editedView("POST", "/createView", ""));
        assertEquals("app", EditOrder.editedItem("POST", "/view/team/job/app/configSubmit", null));
        assertEquals("f/app", EditOrder.editedItem("POST", "/view/team/view/inner/job/f/job/app/disable", null));
        assertEquals("new", EditOrder.editedItem("POST", "/view/team/createItem", "new"));
        assertNull(EditOrder.editedItem("POST", "/view/team/configSubmit", null));
        assertEquals(new EditOrder.Move("app", "svc"), EditOrder.moved("POST", "/view/team/job/app/confirmRename", "svc", null));
        assertEquals("app", OwnerRouting.target("POST", "/view/team/job/app/build").job());
    }
}
