package homesync

import (
	"context"
	"errors"
	"os"
	"sync"
	"testing"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"
)

// These tests need PostgreSQL: NETCI_QUEUE_TEST_DATABASE_URL, a database they may empty.
func testStore(t *testing.T) *Store {
	t.Helper()
	url := os.Getenv("NETCI_QUEUE_TEST_DATABASE_URL")
	if url == "" {
		t.Skip("NETCI_QUEUE_TEST_DATABASE_URL is not set: the homesync PostgreSQL tests did not run")
	}
	ctx := context.Background()
	pool, err := pgxpool.New(ctx, url)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(pool.Close)
	if err := Migrate(ctx, pool); err != nil {
		t.Fatal(err)
	}
	if _, err := pool.Exec(ctx, `TRUNCATE home_items, home_changes, home_locks, home_job_owners, home_build_numbers`); err != nil {
		t.Fatal(err)
	}
	return &Store{Pool: pool}
}

func TestAnItemIsCreatedChangedAndDeletedInVersions(t *testing.T) {
	s, ctx := testStore(t), context.Background()
	v, err := s.Put(ctx, "team/app", []byte("<flow-definition/>"), 0, "r0", 0)
	if err != nil || v != 1 {
		t.Fatalf("create: %d %v", v, err)
	}
	if v, err = s.Put(ctx, "team/app", []byte("<flow-definition>2</flow-definition>"), 1, "r1", 0); err != nil || v != 2 {
		t.Fatalf("change: %d %v", v, err)
	}
	it, ok, err := s.Get(ctx, "team/app")
	if err != nil || !ok || it.Version != 2 || string(it.Content) != "<flow-definition>2</flow-definition>" || it.UpdatedBy != "r1" || it.SHA256 == "" {
		t.Fatalf("get: %+v %v %v", it, ok, err)
	}
	if v, err = s.Delete(ctx, "team/app", 2, "r0", 0); err != nil || v != 3 {
		t.Fatalf("delete: %d %v", v, err)
	}
	it, _, _ = s.Get(ctx, "team/app")
	if !it.Deleted || it.Content != nil {
		t.Fatalf("a deleted item keeps a tombstone without content: %+v", it)
	}
	// Re-created from the tombstone's version.
	if v, err = s.Put(ctx, "team/app", []byte("<again/>"), 3, "r1", 0); err != nil || v != 4 {
		t.Fatalf("re-create: %d %v", v, err)
	}
	changes, head, err := s.Changes(ctx, 0, 0)
	if err != nil || len(changes) != 4 || head != changes[3].Seq {
		t.Fatalf("feed: %+v head %d %v", changes, head, err)
	}
	for i, want := range []struct {
		v       int64
		deleted bool
		origin  string
	}{{1, false, "r0"}, {2, false, "r1"}, {3, true, "r0"}, {4, false, "r1"}} {
		if c := changes[i]; c.Version != want.v || c.Deleted != want.deleted || c.Origin != want.origin {
			t.Fatalf("change %d: %+v", i, c)
		}
	}
}

// ADR-074 E4: two controllers saved one job and the second silently replaced the first. Here
// the second, made from the same version, is refused and told the version it is behind.
func TestAChangeFromAStaleVersionIsRefusedNotApplied(t *testing.T) {
	s, ctx := testStore(t), context.Background()
	if _, err := s.Put(ctx, "quick", []byte("v1"), 0, "r0", 0); err != nil {
		t.Fatal(err)
	}
	if _, err := s.Put(ctx, "quick", []byte("description from r0"), 1, "r0", 0); err != nil {
		t.Fatal(err)
	}
	_, err := s.Put(ctx, "quick", []byte("disabled by r1, from v1"), 1, "r1", 0)
	var conflict *ConflictError
	if !errors.As(err, &conflict) || conflict.Current != 2 {
		t.Fatalf("want a conflict at version 2, got %v", err)
	}
	it, _, _ := s.Get(ctx, "quick")
	if string(it.Content) != "description from r0" {
		t.Fatalf("the refused change was applied: %q", it.Content)
	}
	if _, err := s.Put(ctx, "new", []byte("x"), 5, "r0", 0); !errors.As(err, &conflict) || conflict.Current != 0 {
		t.Fatalf("an item that does not exist is at version 0: %v", err)
	}
	if _, ok, _ := s.Get(ctx, "new"); ok {
		t.Fatal("a refused create left a row")
	}
}

func TestConcurrentChangesFromOneVersionMakeExactlyOneWinner(t *testing.T) {
	s, ctx := testStore(t), context.Background()
	if _, err := s.Put(ctx, "race", []byte("v1"), 0, "r0", 0); err != nil {
		t.Fatal(err)
	}
	const writers = 16
	var wg sync.WaitGroup
	results := make(chan error, writers)
	for i := 0; i < writers; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			_, err := s.Put(ctx, "race", []byte{byte('a' + i)}, 1, []string{"r0", "r1"}[i%2], 0)
			results <- err
		}(i)
	}
	wg.Wait()
	close(results)
	won, refused := 0, 0
	for err := range results {
		var conflict *ConflictError
		switch {
		case err == nil:
			won++
		case errors.As(err, &conflict) && conflict.Current == 2:
			refused++
		default:
			t.Fatalf("unexpected: %v", err)
		}
	}
	if won != 1 || refused != writers-1 {
		t.Fatalf("won %d refused %d", won, refused)
	}
	changes, _, _ := s.Changes(ctx, 0, 0)
	if len(changes) != 2 {
		t.Fatalf("one change per accepted write, got %d", len(changes))
	}
}

func TestALockOrdersOneReplicasEditAndFencesAStaleHolder(t *testing.T) {
	s, ctx := testStore(t), context.Background()
	if _, err := s.Put(ctx, "app", []byte("v1"), 0, "r0", 0); err != nil {
		t.Fatal(err)
	}
	tok, err := s.Lock(ctx, "app", "r0", 2*time.Second)
	if err != nil {
		t.Fatal(err)
	}
	var locked *LockedError
	if _, err := s.Lock(ctx, "app", "r1", time.Second); !errors.As(err, &locked) || locked.Holder != "r0" {
		t.Fatalf("a second replica took a held lock: %v", err)
	}
	// While r0 holds it, a change without the lock (a background save on r1) is refused.
	if _, err := s.Put(ctx, "app", []byte("bg"), 1, "r1", 0); !errors.Is(err, ErrLocked) {
		t.Fatalf("an unlocked change got past a held lock: %v", err)
	}
	if v, err := s.Put(ctx, "app", []byte("r0 edit"), 1, "r0", tok); err != nil || v != 2 {
		t.Fatalf("the holder's change: %d %v", v, err)
	}

	// The lock expires; r1 takes it; r0's old token no longer publishes anything.
	time.Sleep(2100 * time.Millisecond)
	tok1, err := s.Lock(ctx, "app", "r1", 5*time.Second)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := s.Put(ctx, "app", []byte("late r0"), 2, "r0", tok); !errors.Is(err, ErrLockLost) {
		t.Fatalf("a stale holder published: %v", err)
	}
	if _, err := s.Put(ctx, "app", []byte("r1 edit"), 2, "r1", tok1); err != nil {
		t.Fatal(err)
	}
	if err := s.Unlock(ctx, "app", "r0", tok); err != nil {
		t.Fatal(err)
	}
	if _, err := s.Lock(ctx, "app", "r0", time.Second); !errors.As(err, &locked) {
		t.Fatal("unlocking with a stale token released somebody else's lock")
	}
	if err := s.Unlock(ctx, "app", "r1", tok1); err != nil {
		t.Fatal(err)
	}
	if _, err := s.Lock(ctx, "app", "r0", time.Second); err != nil {
		t.Fatalf("a released lock could not be taken: %v", err)
	}
}

func TestConcurrentLocksHaveOneHolder(t *testing.T) {
	s, ctx := testStore(t), context.Background()
	var wg sync.WaitGroup
	var mu sync.Mutex
	holders := map[string]bool{}
	for i := 0; i < 12; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			who := []string{"r0", "r1", "r2"}[i%3]
			if _, err := s.Lock(ctx, "contended", who, 10*time.Second); err == nil {
				mu.Lock()
				holders[who] = true
				mu.Unlock()
			}
		}(i)
	}
	wg.Wait()
	if len(holders) != 1 {
		t.Fatalf("holders: %v", holders)
	}
}
