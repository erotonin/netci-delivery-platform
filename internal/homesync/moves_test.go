package homesync

import (
	"context"
	"errors"
	"testing"
	"time"
)

func mustPut(t *testing.T, s *Store, path, content string) {
	t.Helper()
	if _, err := s.Put(context.Background(), path, []byte(content), 0, "r0", 0); err != nil {
		t.Fatalf("put %s: %v", path, err)
	}
}

// ageReservations makes every reservation older than the busy margin: a build whose number was
// reserved minutes ago and that has a finished record, or never started, is not running.
func ageReservations(t *testing.T, s *Store) {
	t.Helper()
	if _, err := s.Pool.Exec(context.Background(), `UPDATE home_build_numbers SET reserved_at = now() - interval '10 minutes'`); err != nil {
		t.Fatal(err)
	}
}

func TestAMoveCarriesTheItemItsChildrenItsBuildsAndItsNumbers(t *testing.T) {
	s, ctx := testStore(t), context.Background()
	mustPut(t, s, "f", "<folder/>")
	mustPut(t, s, "f/app", "<flow-definition/>")
	mustPut(t, s, "@builds/f/app/1", "build-1")
	mustPut(t, s, "@builds/f/app/2", "build-2")
	// Names that only start with the same letters are someone else's.
	mustPut(t, s, "fx", "<folder>fx</folder>")
	mustPut(t, s, "@builds/fx/1", "fx-1")
	o, err := s.ClaimJob(ctx, "f/app", "r0", time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	for n := 1; n <= 2; n++ {
		if err := s.Reserve(ctx, "f/app", n, "r0", o.Epoch); err != nil {
			t.Fatal(err)
		}
	}
	head, _ := s.Head(ctx)
	from, err := s.Lock(ctx, "f", "r1", time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	to, err := s.Lock(ctx, "g", "r1", time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	res, err := s.Move(ctx, "f", "g", "r1", from, to, false)
	if err != nil || res.Moved != 4 || res.Version != 1 || res.FromVersion != 2 || res.SHA256 == "" {
		t.Fatalf("move: %+v %v", res, err)
	}
	// Released, as the replica does when its request ends: until then the moved job waits.
	var locked *LockedError
	if err := s.Reserve(ctx, "g/app", 3, "r0", o.Epoch); !errors.As(err, &locked) {
		t.Fatalf("a build of the moved job before the move's locks are released: %v", err)
	}
	_ = s.Unlock(ctx, "f", "r1", from)
	_ = s.Unlock(ctx, "g", "r1", to)
	for path, want := range map[string]string{"g": "<folder/>", "g/app": "<flow-definition/>", "@builds/g/app/1": "build-1",
		"@builds/g/app/2": "build-2", "fx": "<folder>fx</folder>", "@builds/fx/1": "fx-1"} {
		it, ok, err := s.Get(ctx, path)
		if err != nil || !ok || it.Deleted || string(it.Content) != want {
			t.Errorf("%s after the move: %+v %v %v", path, it, ok, err)
		}
	}
	for _, path := range []string{"f", "f/app", "@builds/f/app/1", "@builds/f/app/2"} {
		if it, _, _ := s.Get(ctx, path); !it.Deleted {
			t.Errorf("%s is still there after the move: %+v", path, it)
		}
	}
	changes, _, err := s.Changes(ctx, head, 100)
	if err != nil || len(changes) != 8 {
		t.Fatalf("feed: %d entries %v", len(changes), err)
	}
	// The old name's tombstone first, saying where it went; then parents before children, builds last.
	if c := changes[0]; c.Path != "f" || !c.Deleted || c.MovedTo != "g" {
		t.Fatalf("the move does not start with the old name's tombstone: %+v", c)
	}
	if changes[1].Path != "g" || changes[2].Path != "g/app" || changes[3].Path != "@builds/g/app/1" {
		t.Fatalf("order: %+v", changes[1:4])
	}
	for _, c := range changes[1:] {
		if c.MovedTo != "" {
			t.Fatalf("only the moved item's tombstone names the destination: %+v", c)
		}
	}
	// The lease and the numbers went with the job: its owner keeps building it where it was.
	if got, _ := s.JobOwner(ctx, "g/app"); got.Owner != "r0" || got.Epoch != o.Epoch {
		t.Fatalf("lease after the move: %+v", got)
	}
	var taken *NumberTakenError
	if err := s.Reserve(ctx, "g/app", 2, "r0", o.Epoch); !errors.As(err, &taken) || taken.Max != 2 {
		t.Fatalf("numbering did not continue under the new name: %v", err)
	}
	if err := s.Reserve(ctx, "g/app", 3, "r0", o.Epoch); err != nil {
		t.Fatalf("the next number under the new name: %v", err)
	}
	if max, _ := s.MaxReserved(ctx, "f/app"); max != 0 {
		t.Fatalf("the old name kept its numbers: %d", max)
	}
	// A replica that has not applied the move yet cannot take the old name to build it.
	if _, err := s.ClaimJob(ctx, "f/app", "r1", time.Minute); !errors.Is(err, ErrJobGone) {
		t.Fatalf("claim of the old name: %v", err)
	}
}

func TestAMoveIsRefusedWhileABuildUnderItRunsAnywhere(t *testing.T) {
	s, ctx := testStore(t), context.Background()
	mustPut(t, s, "f", "<folder/>")
	mustPut(t, s, "f/app", "<flow-definition/>")
	o, _ := s.ClaimJob(ctx, "f/app", "r0", time.Minute)
	if err := s.Reserve(ctx, "f/app", 1, "r0", o.Epoch); err != nil {
		t.Fatal(err)
	}
	var busy *BusyError
	// Just given its number, no copy yet: about to start.
	if _, err := s.Move(ctx, "f", "g", "r1", 0, 0, true); !errors.As(err, &busy) || busy.Job != "f/app" || busy.Number != 1 || busy.Replica != "r0" {
		t.Fatalf("a build just given its number: %v", err)
	}
	ageReservations(t, s)
	// Running: its copy is there.
	mustPut(t, s, "@running/f/app/1", "copy")
	if _, err := s.Move(ctx, "f", "g", "r1", 0, 0, true); !errors.As(err, &busy) {
		t.Fatalf("a running build: %v", err)
	}
	// Its copy just deleted, its finished record not there yet: finishing.
	if _, err := s.Delete(ctx, "@running/f/app/1", 1, "r0", 0); err != nil {
		t.Fatal(err)
	}
	if _, err := s.Move(ctx, "f", "g", "r1", 0, 0, true); !errors.As(err, &busy) {
		t.Fatalf("a build between its copy's deletion and its finished record: %v", err)
	}
	mustPut(t, s, "@builds/f/app/1", "build-1")
	head, _ := s.Head(ctx)
	if _, err := s.Move(ctx, "f", "g", "r1", 0, 0, true); err != nil {
		t.Fatalf("a finished build stops nothing: %v", err)
	}
	if now, _ := s.Head(ctx); now != head {
		t.Fatalf("a dry run wrote %d feed entries", now-head)
	}
	if it, _, _ := s.Get(ctx, "g"); it.Version != 0 {
		t.Fatalf("a dry run made the destination: %+v", it)
	}
}

func TestAMoveIsRefusedOntoAnItemFromNothingAndIntoItself(t *testing.T) {
	s, ctx := testStore(t), context.Background()
	mustPut(t, s, "a", "<a/>")
	mustPut(t, s, "b", "<b/>")
	var exists *ExistsError
	if _, err := s.Move(ctx, "a", "b", "r0", 0, 0, false); !errors.As(err, &exists) {
		t.Fatalf("onto an item: %v", err)
	}
	if _, err := s.Move(ctx, "nothing", "c", "r0", 0, 0, false); !errors.Is(err, ErrNoItem) {
		t.Fatalf("from nothing: %v", err)
	}
	for _, m := range [][2]string{{"a", "a"}, {"a", "a/inner"}, {"@builds/a/1", "c"}, {"a", "@users/x"}} {
		if _, err := s.Move(ctx, m[0], m[1], "r0", 0, 0, false); !errors.Is(err, ErrBadMove) {
			t.Fatalf("%s -> %s: %v", m[0], m[1], err)
		}
	}
	// Onto a deleted item: its tombstone's version goes on.
	if _, err := s.Delete(ctx, "b", 1, "r0", 0); err != nil {
		t.Fatal(err)
	}
	if res, err := s.Move(ctx, "a", "b", "r0", 0, 0, false); err != nil || res.Version != 3 {
		t.Fatalf("onto a deleted item: %+v %v", res, err)
	}
}

func TestAMoveRespectsEveryoneElsesLocks(t *testing.T) {
	s, ctx := testStore(t), context.Background()
	mustPut(t, s, "f", "<folder/>")
	mustPut(t, s, "f/app", "<flow-definition/>")
	child, err := s.Lock(ctx, "f/app", "r0", time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	var locked *LockedError
	if _, err := s.Move(ctx, "f", "g", "r1", 0, 0, false); !errors.As(err, &locked) || locked.Holder != "r0" {
		t.Fatalf("a child being edited on another replica: %v", err)
	}
	if err := s.Unlock(ctx, "f/app", "r0", child); err != nil {
		t.Fatal(err)
	}
	if _, err := s.Lock(ctx, "g", "r0", time.Minute); err != nil {
		t.Fatal(err)
	}
	if _, err := s.Move(ctx, "f", "g", "r1", 0, 0, false); !errors.As(err, &locked) {
		t.Fatalf("the destination being created on another replica: %v", err)
	}
	stale, err := s.Lock(ctx, "f", "r1", time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := s.Lock(ctx, "f", "r1", time.Minute); err != nil { // renewed: the first token is fenced
		t.Fatal(err)
	}
	if _, err := s.Move(ctx, "f", "h", "r1", stale, 0, false); !errors.Is(err, ErrLockLost) {
		t.Fatalf("a move under a lock no longer held: %v", err)
	}
}

func TestABuildWaitsWhileItsJobOrFolderIsBeingChangedElsewhere(t *testing.T) {
	s, ctx := testStore(t), context.Background()
	o, _ := s.ClaimJob(ctx, "f/app", "r0", time.Minute)
	token, err := s.Lock(ctx, "f", "r1", time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	var locked *LockedError
	if err := s.Reserve(ctx, "f/app", 1, "r0", o.Epoch); !errors.As(err, &locked) || locked.Holder != "r1" {
		t.Fatalf("a reservation while the folder is being changed: %v", err)
	}
	// The owner's own edit does not hold its own builds back.
	if _, err := s.Lock(ctx, "f/app", "r0", time.Minute); err != nil {
		t.Fatal(err)
	}
	if err := s.Unlock(ctx, "f", "r1", token); err != nil {
		t.Fatal(err)
	}
	if err := s.Reserve(ctx, "f/app", 1, "r0", o.Epoch); err != nil {
		t.Fatalf("after the edit: %v", err)
	}
}

// A claim and a reservation of the old name racing the move: each either comes before it, and is
// carried to the new name, or after it, and is refused -- never a lease on a name that is gone.
func TestAClaimOrReservationRacingAMoveNeverHoldsTheOldName(t *testing.T) {
	s, ctx := testStore(t), context.Background()
	mustPut(t, s, "app", "<flow-definition/>")
	o, _ := s.ClaimJob(ctx, "app", "r0", time.Minute)
	if err := s.Reserve(ctx, "app", 1, "r0", o.Epoch); err != nil {
		t.Fatal(err)
	}
	mustPut(t, s, "@builds/app/1", "build-1")
	ageReservations(t, s)
	claimed := make(chan error, 1)
	reserved := make(chan error, 1)
	beforeCommit = func(string) {
		go func() { _, err := s.ClaimJob(ctx, "app", "r1", time.Minute); claimed <- err }()
		go func() { reserved <- s.Reserve(ctx, "app", 2, "r0", o.Epoch) }()
		select {
		case err := <-claimed:
			t.Errorf("a claim of the old name did not wait for the move: %v", err)
		case err := <-reserved:
			t.Errorf("a reservation of the old name did not wait for the move: %v", err)
		case <-time.After(300 * time.Millisecond):
		}
	}
	t.Cleanup(func() { beforeCommit = nil })
	if _, err := s.Move(ctx, "app", "renamed", "r1", 0, 0, false); err != nil {
		t.Fatal(err)
	}
	beforeCommit = nil
	if err := <-claimed; !errors.Is(err, ErrJobGone) {
		t.Fatalf("claim after the move: %v", err)
	}
	if err := <-reserved; !errors.Is(err, ErrNotOwner) {
		t.Fatalf("reservation after the move: %v", err)
	}
	if err := s.Reserve(ctx, "renamed", 2, "r0", o.Epoch); err != nil {
		t.Fatalf("the owner builds the job under its new name: %v", err)
	}
}
