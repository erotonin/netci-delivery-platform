-- Which team owns an application.
--
-- Roles alone are global: before this, any developer could run any team's pipeline and any
-- reviewer could approve any team's production release. That is workable for one team and
-- wrong for an organisation, where "who may deploy this" is a property of the application,
-- not only of the person.
--
-- Existing rows have no owner, and are left that way rather than being assigned to a team
-- nobody chose. An unowned application keeps behaving exactly as it does today -- role
-- checks apply, team checks do not -- so this migration changes no existing behaviour.
-- Set NETCI_REQUIRE_APPLICATION_OWNER=true once every application has an owner, and an
-- unowned one then becomes platform-admin only. That is the migration path: adopt
-- gradually, then close the door.
ALTER TABLE applications ADD COLUMN IF NOT EXISTS owner_team VARCHAR(255);

COMMENT ON COLUMN applications.owner_team IS
    'Team that owns this application. NULL means unowned: role checks still apply, team checks do not.';

-- Listing "my team''s applications" is the common Portal query once ownership is in use.
CREATE INDEX IF NOT EXISTS applications_owner_team_idx ON applications (owner_team);
