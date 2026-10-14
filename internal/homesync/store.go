// Package homesync keeps the replicas' JENKINS_HOMEs the same (ADR-076). Each replica has its own
// home on its own disk; this is the order every change to an item (a job, a folder, a view) is
// made in. A change is a compare-and-set on the item's version, appended to a feed every replica
// follows, so two replicas never overwrite each other silently: one wins, the other is refused.
package homesync

import (
	"context"
	"crypto/sha256"
	"embed"
	"encoding/hex"
	"errors"
	"fmt"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/erotonin/netci-delivery-platform/internal/pgmigrate"
)

//go:embed migrations/*.sql
var migrations embed.FS

// Migrate applies the store's migrations.
func Migrate(ctx context.Context, pool *pgxpool.Pool) error {
	return pgmigrate.Apply(ctx, pool, migrations, "migrations")
}

// Notify is the channel a committed change is announced on; the feed's long polls wait on it.
const Notify = "home_changes"

// feedLock is the advisory lock every change takes before it is given its place in the feed.
//
// home_changes.seq comes from a sequence, handed out when the row is inserted, and transactions
// commit in any order: one holding seq N could commit after one holding N+1. A replica reading the
// feed then saw N+1, moved past it, and never read N -- the store failover run lost an
// acknowledged create on one replica exactly so (r1's st-043727-r1-2, seq 3205), silently, with
// synchronous replication making each commit long enough to be overtaken. Taken last, after the
// item's row lock, and held to the commit, it makes the feed's order its commit order: a
// committed N+1 means N is committed or will never exist, so what a reader sees is always a prefix.
const feedLock int64 = 0x6e6574636966 // "netcif"

// beforeCommit, when set by a test, runs in a change after it has its place in the feed and
// before it commits: the moment another change could overtake it.
var beforeCommit func(path string)

var (
	// ErrLocked: another replica holds the item's lock.
	ErrLocked = errors.New("the item is locked by another replica")
	// ErrLockLost: the caller's lock expired, or was taken over, before it published.
	ErrLockLost = errors.New("the lock this change was made under is no longer held")
)

// ConflictError: the item is not at the version the change was made from.
type ConflictError struct{ Current int64 }

func (e *ConflictError) Error() string {
	return fmt.Sprintf("the item is at version %d, not the version this change was made from", e.Current)
}

// LockedError names who holds the lock.
type LockedError struct{ Holder string }

func (e *LockedError) Error() string { return "the item is locked by " + e.Holder }
func (e *LockedError) Unwrap() error { return ErrLocked }

// Item is one item's current state.
type Item struct {
	Path      string
	Version   int64
	Deleted   bool
	Content   []byte
	SHA256    string
	UpdatedBy string
	UpdatedAt time.Time
}

// Change is one entry of the feed.
type Change struct {
	Seq     int64     `json:"seq"`
	Path    string    `json:"path"`
	Version int64     `json:"version"`
	Deleted bool      `json:"deleted"`
	Origin  string    `json:"origin"`
	At      time.Time `json:"at"`
}

// Store is the PostgreSQL behind every netci-homesync replica; they share nothing else.
type Store struct{ Pool *pgxpool.Pool }

// Get returns an item, or false if it never existed.
func (s *Store) Get(ctx context.Context, path string) (Item, bool, error) {
	var it Item
	var sum *string
	err := s.Pool.QueryRow(ctx, `SELECT path, version, deleted, content, sha256, updated_by, updated_at
		FROM home_items WHERE path = $1`, path).Scan(&it.Path, &it.Version, &it.Deleted, &it.Content, &sum, &it.UpdatedBy, &it.UpdatedAt)
	if errors.Is(err, pgx.ErrNoRows) {
		return Item{Path: path}, false, nil
	}
	if sum != nil {
		it.SHA256 = *sum
	}
	return it, err == nil, err
}

// Put writes an item's content if it is at expect (0: it must not exist, or be deleted at 0).
// lockToken 0 is a change made without the lock (a save no request asked for): it is refused
// while another replica holds the lock, and otherwise still has to win the compare-and-set.
func (s *Store) Put(ctx context.Context, path string, content []byte, expect int64, origin string, lockToken int64) (int64, error) {
	return s.change(ctx, path, content, false, expect, origin, lockToken)
}

// Delete marks an item deleted, under the same rules as Put. The row stays as a tombstone so a
// replica that missed the delete is told, and a re-create starts from its version.
func (s *Store) Delete(ctx context.Context, path string, expect int64, origin string, lockToken int64) (int64, error) {
	return s.change(ctx, path, nil, true, expect, origin, lockToken)
}

func (s *Store) change(ctx context.Context, path string, content []byte, deleted bool, expect int64, origin string, lockToken int64) (int64, error) {
	if path == "" || origin == "" {
		return 0, errors.New("path and origin are required")
	}
	tx, err := s.Pool.BeginTx(ctx, pgx.TxOptions{})
	if err != nil {
		return 0, err
	}
	defer tx.Rollback(context.Background()) //nolint:errcheck

	// The item's row, locked for the rest of the transaction: concurrent changes of one item
	// queue here and each sees the version the previous one wrote.
	if _, err := tx.Exec(ctx, `INSERT INTO home_items (path, version, deleted, updated_by)
		VALUES ($1, 0, true, $2) ON CONFLICT (path) DO NOTHING`, path, origin); err != nil {
		return 0, err
	}
	var current int64
	if err := tx.QueryRow(ctx, `SELECT version FROM home_items WHERE path = $1 FOR UPDATE`, path).Scan(&current); err != nil {
		return 0, err
	}
	if err := checkLock(ctx, tx, path, origin, lockToken); err != nil {
		return 0, err
	}
	if current != expect {
		return 0, &ConflictError{Current: current}
	}
	next := current + 1
	var sum *string
	if !deleted {
		h := sha256.Sum256(content)
		v := hex.EncodeToString(h[:])
		sum = &v
	} else {
		content = nil
	}
	if _, err := tx.Exec(ctx, `UPDATE home_items SET version = $2, deleted = $3, content = $4, sha256 = $5,
		updated_by = $6, updated_at = now() WHERE path = $1`, path, next, deleted, content, sum, origin); err != nil {
		return 0, err
	}
	if _, err := tx.Exec(ctx, `SELECT pg_advisory_xact_lock($1)`, feedLock); err != nil {
		return 0, err
	}
	var seq int64
	if err := tx.QueryRow(ctx, `INSERT INTO home_changes (path, version, deleted, origin) VALUES ($1, $2, $3, $4)
		RETURNING seq`, path, next, deleted, origin).Scan(&seq); err != nil {
		return 0, err
	}
	if _, err := tx.Exec(ctx, `SELECT pg_notify($1, $2)`, Notify, fmt.Sprint(seq)); err != nil {
		return 0, err
	}
	if beforeCommit != nil {
		beforeCommit(path)
	}
	return next, tx.Commit(ctx)
}

// checkLock: with a token, it must be the caller's unexpired lock; without one, nobody else may
// hold a live lock on the item. Expiry is the database's clock, the one every replica shares.
func checkLock(ctx context.Context, tx pgx.Tx, path, origin string, token int64) error {
	var holder string
	var held int64
	var live bool
	err := tx.QueryRow(ctx, `SELECT holder, token, expires_at > now() FROM home_locks WHERE path = $1`, path).Scan(&holder, &held, &live)
	if errors.Is(err, pgx.ErrNoRows) {
		if token != 0 {
			return ErrLockLost
		}
		return nil
	}
	if err != nil {
		return err
	}
	if token != 0 {
		if holder != origin || held != token || !live {
			return ErrLockLost
		}
		return nil
	}
	if live && holder != origin {
		return &LockedError{Holder: holder}
	}
	return nil
}

// ErrBadTTL is a lock asked for with a lifetime outside (0, 5 minutes]: the caller's mistake, unlike
// every other error of Lock, which is the store's.
var ErrBadTTL = errors.New("a lock lasts more than 0 and at most 5 minutes")

// Lock takes the item's lock for ttl, or renews the caller's own. A new token each time, so a
// renewal also fences any older use of the caller's previous token.
func (s *Store) Lock(ctx context.Context, path, holder string, ttl time.Duration) (int64, error) {
	if ttl <= 0 || ttl > 5*time.Minute {
		return 0, ErrBadTTL
	}
	var token int64
	err := s.Pool.QueryRow(ctx, `INSERT INTO home_locks (path, holder, token, expires_at)
		VALUES ($1, $2, nextval('home_lock_tokens'), now() + $3 * interval '1 millisecond')
		ON CONFLICT (path) DO UPDATE SET holder = EXCLUDED.holder, token = EXCLUDED.token, expires_at = EXCLUDED.expires_at
		WHERE home_locks.expires_at <= now() OR home_locks.holder = EXCLUDED.holder
		RETURNING token`, path, holder, ttl.Milliseconds()).Scan(&token)
	if errors.Is(err, pgx.ErrNoRows) {
		var other string
		if err := s.Pool.QueryRow(ctx, `SELECT holder FROM home_locks WHERE path = $1`, path).Scan(&other); err != nil && !errors.Is(err, pgx.ErrNoRows) {
			return 0, err
		}
		return 0, &LockedError{Holder: other}
	}
	return token, err
}

// Unlock releases the caller's lock; a lock it no longer holds is left alone.
func (s *Store) Unlock(ctx context.Context, path, holder string, token int64) error {
	_, err := s.Pool.Exec(ctx, `DELETE FROM home_locks WHERE path = $1 AND holder = $2 AND token = $3`, path, holder, token)
	return err
}

// Changes returns up to limit feed entries after seq, in order, and the feed's head.
func (s *Store) Changes(ctx context.Context, after int64, limit int) ([]Change, int64, error) {
	if limit <= 0 || limit > 1000 {
		limit = 1000
	}
	rows, err := s.Pool.Query(ctx, `SELECT seq, path, version, deleted, origin, at FROM home_changes
		WHERE seq > $1 ORDER BY seq LIMIT $2`, after, limit)
	if err != nil {
		return nil, 0, err
	}
	changes, err := pgx.CollectRows(rows, func(r pgx.CollectableRow) (Change, error) {
		var c Change
		return c, r.Scan(&c.Seq, &c.Path, &c.Version, &c.Deleted, &c.Origin, &c.At)
	})
	if err != nil {
		return nil, 0, err
	}
	head, err := s.Head(ctx)
	return changes, head, err
}

// CompactedThrough is the highest seq compaction has deleted: a reader below it re-reads all.
func (s *Store) CompactedThrough(ctx context.Context) (int64, error) {
	var n int64
	err := s.Pool.QueryRow(ctx, `SELECT compacted_through FROM home_feed WHERE id = 1`).Scan(&n)
	return n, err
}

// Compact deletes, among changes older than olderThan, those a later change of the same path has
// superseded, and every change and the tombstone of a running build's copy whose build has ended.
// It returns how many changes were deleted. The latest change of every other path is kept.
func (s *Store) Compact(ctx context.Context, olderThan time.Duration) (int64, error) {
	tx, err := s.Pool.BeginTx(ctx, pgx.TxOptions{})
	if err != nil {
		return 0, err
	}
	defer tx.Rollback(context.Background()) //nolint:errcheck
	// The feed lock: no change is placed in the feed meanwhile, so "superseded" is decided on a
	// prefix of the feed (the same lock every change takes before its insert).
	if _, err := tx.Exec(ctx, `SELECT pg_advisory_xact_lock($1)`, feedLock); err != nil {
		return 0, err
	}
	cutoff := fmt.Sprintf("%d milliseconds", olderThan.Milliseconds())
	var deleted, through int64
	err = tx.QueryRow(ctx, `WITH gone AS (
			DELETE FROM home_changes c
			WHERE c.at < now() - $1::interval
			  AND (EXISTS (SELECT 1 FROM home_changes later WHERE later.path = c.path AND later.seq > c.seq)
			       OR (c.path LIKE '@running/%' AND EXISTS (
			             SELECT 1 FROM home_items i WHERE i.path = c.path AND i.deleted AND i.updated_at < now() - $1::interval)))
			RETURNING seq)
		SELECT count(*), COALESCE(max(seq), 0) FROM gone`, cutoff).Scan(&deleted, &through)
	if err != nil {
		return 0, err
	}
	if _, err := tx.Exec(ctx, `DELETE FROM home_items WHERE path LIKE '@running/%' AND deleted AND updated_at < now() - $1::interval`, cutoff); err != nil {
		return 0, err
	}
	if _, err := tx.Exec(ctx, `UPDATE home_feed SET compacted_through = greatest(compacted_through, $1) WHERE id = 1`, through); err != nil {
		return 0, err
	}
	return deleted, tx.Commit(ctx)
}

// Head is the last seq of the feed (0 when empty).
func (s *Store) Head(ctx context.Context) (int64, error) {
	var head int64
	err := s.Pool.QueryRow(ctx, `SELECT COALESCE(max(seq), 0) FROM home_changes`).Scan(&head)
	return head, err
}
