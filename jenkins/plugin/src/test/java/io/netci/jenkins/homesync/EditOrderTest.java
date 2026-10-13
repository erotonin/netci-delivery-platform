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
    }

    @Test
    void readsBuildsAndOtherPagesAreNotEdits() {
        assertNull(EditOrder.editedItem("GET", "/job/app/config.xml", null));
        assertNull(EditOrder.editedItem("POST", "/job/app/build", null));
        assertNull(EditOrder.editedItem("POST", "/job/app/12/stop", null));
        assertNull(EditOrder.editedItem("POST", "/job/app/job/x", null));
        assertNull(EditOrder.editedItem("POST", "/configSubmit", null)); // global config: JCasC's, not an item
        assertNull(EditOrder.editedItem("POST", "/createItem", ""));
    }
}
