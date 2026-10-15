-- ADR-087: an item renamed or moved is one change of the store, not a delete and a create. The
-- tombstone of the old name says where the item went, so a replica that holds it renames its own
-- copy -- builds and all -- instead of deleting it and fetching every build's archive again.
ALTER TABLE home_changes ADD COLUMN moved_to text;
