package io.netci.jenkins.homesync;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNull;

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
}
