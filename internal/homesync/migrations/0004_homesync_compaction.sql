-- The feed is compacted (ADR-083): a change that a later change of the same path has superseded,
-- older than a bound, is deleted, as is everything of a running build's copy once the build has
-- ended (its last change is the copy's deletion). The latest change of every other path stays, so
-- a replica reading the feed from the start still learns every item. compacted_through is the
-- highest seq deleted: a replica whose position is below it may have missed an entry and must
-- read everything again rather than go on.
CREATE INDEX home_changes_path ON home_changes (path, seq);
CREATE TABLE home_feed (
    id                int    PRIMARY KEY CHECK (id = 1),
    compacted_through bigint NOT NULL DEFAULT 0
);
INSERT INTO home_feed (id) VALUES (1);
