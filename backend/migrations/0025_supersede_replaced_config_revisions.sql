-- Activation now marks the revision it replaces as `superseded`. Rows written before
-- that fix are still `active` although the module points elsewhere; the browser showed
-- every one of them as "Active". One-off repair; the invariant is enforced in code.
UPDATE module_config_revisions r
   SET status = 'superseded'
  FROM modules m
 WHERE r.module_id = m.id
   AND r.status = 'active'
   AND m.active_config_revision_id IS NOT NULL
   AND r.id <> m.active_config_revision_id;
