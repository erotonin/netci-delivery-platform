package homesync

import (
	"context"
	"errors"
	"fmt"
	"sync"
	"testing"
	"time"
)

func TestAJobHasOneOwnerAndATakeoverGetsANewEpoch(t *testing.T) {
	s, ctx := testStore(t), context.Background()
	o, err := s.ClaimJob(ctx, "app", "r0", 2*time.Second)
	if err != nil || o.Owner != "r0" || o.Epoch == 0 {
		t.Fatalf("claim: %+v %v", o, err)
	}
	var owned *OwnedError
	if _, err := s.ClaimJob(ctx, "app", "r1", time.Second); !errors.As(err, &owned) || owned.Ownership.Owner != "r0" {
		t.Fatalf("a second owner: %v", err)
	}
	renewed, err := s.ClaimJob(ctx, "app", "r0", 2*time.Second)
	if err != nil || renewed.Epoch != o.Epoch {
		t.Fatalf("a renewal changed the epoch: %+v %v", renewed, err)
	}
	time.Sleep(2100 * time.Millisecond)
	taken, err := s.ClaimJob(ctx, "app", "r1", 5*time.Second)
	if err != nil || taken.Owner != "r1" || taken.Epoch <= o.Epoch {
		t.Fatalf("takeover after expiry: %+v %v", taken, err)
	}
	if _, err := s.ClaimJob(ctx, "app", "r0", time.Second); !errors.As(err, &owned) {
		t.Fatalf("the old owner took it back while r1's lease was live: %v", err)
	}
}

func TestConcurrentClaimsMakeOneOwner(t *testing.T) {
	s, ctx := testStore(t), context.Background()
	var wg sync.WaitGroup
	var mu sync.Mutex
	owners := map[string]int64{}
	for i := 0; i < 12; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			who := []string{"r0", "r1", "r2"}[i%3]
			if o, err := s.ClaimJob(ctx, "contended", who, 10*time.Second); err == nil {
				mu.Lock()
				owners[o.Owner] = o.Epoch
				mu.Unlock()
			}
		}(i)
	}
	wg.Wait()
	if len(owners) != 1 {
		t.Fatalf("owners: %v", owners)
	}
}

// ADR-078: a number is reserved only under the live lease; a replica that lost the job cannot
// take one, so a partitioned old owner cannot start a build with a number the new owner uses.
func TestNumbersAreReservedOnceUnderTheLiveLease(t *testing.T) {
	s, ctx := testStore(t), context.Background()
	o, _ := s.ClaimJob(ctx, "app", "r0", 2*time.Second)
	for n := 1; n <= 3; n++ {
		if err := s.Reserve(ctx, "app", n, "r0", o.Epoch); err != nil {
			t.Fatalf("reserve %d: %v", n, err)
		}
	}
	var taken *NumberTakenError
	if err := s.Reserve(ctx, "app", 3, "r0", o.Epoch); !errors.As(err, &taken) || taken.Max != 3 {
		t.Fatalf("a number reserved twice: %v", err)
	}
	if err := s.Reserve(ctx, "app", 4, "r1", o.Epoch); !errors.Is(err, ErrNotOwner) {
		t.Fatalf("a non-owner reserved: %v", err)
	}
	if err := s.Reserve(ctx, "app", 4, "r0", o.Epoch+1); !errors.Is(err, ErrNotOwner) {
		t.Fatalf("a wrong epoch reserved: %v", err)
	}

	time.Sleep(2100 * time.Millisecond) // r0's lease lapses (a pause, a partition)
	if err := s.Reserve(ctx, "app", 4, "r0", o.Epoch); !errors.Is(err, ErrNotOwner) {
		t.Fatalf("an expired lease reserved: %v", err)
	}
	n, _ := s.ClaimJob(ctx, "app", "r1", 5*time.Second)
	max, _ := s.MaxReserved(ctx, "app")
	if max != 3 {
		t.Fatalf("max reserved: %d", max)
	}
	if err := s.Reserve(ctx, "app", max+1, "r1", n.Epoch); err != nil {
		t.Fatalf("the new owner continues the numbers: %v", err)
	}
	if err := s.Reserve(ctx, "app", 5, "r0", o.Epoch); !errors.Is(err, ErrNotOwner) {
		t.Fatalf("the old owner reserved after the takeover: %v", err)
	}
}

func TestConcurrentReservationsOfOneNumberHaveOneWinner(t *testing.T) {
	s, ctx := testStore(t), context.Background()
	o, _ := s.ClaimJob(ctx, "race", "r0", 10*time.Second)
	var wg sync.WaitGroup
	wins := make(chan bool, 16)
	for i := 0; i < 16; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			wins <- s.Reserve(ctx, "race", 1, "r0", o.Epoch) == nil
		}()
	}
	wg.Wait()
	close(wins)
	won := 0
	for w := range wins {
		if w {
			won++
		}
	}
	if won != 1 {
		t.Fatalf("%d reservations of one number succeeded", won)
	}
}

const runA = "6f1b6a8e-3f2c-4c7e-9d2a-0a1b2c3d4e5f"

// ADR-081: a run gets one number across every replica. The run dispatched again while its first
// build was on a dead replica must be told where it went, not given a second build.
func TestARunIsGivenOneNumberAcrossReplicas(t *testing.T) {
	s, ctx := testStore(t), context.Background()
	a, err := s.ClaimJob(ctx, "app", "r0", time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	if err := s.ReserveRun(ctx, "app", 1, "r0", a.Epoch, runA); err != nil {
		t.Fatal(err)
	}
	res, ok, err := s.Run(ctx, runA)
	if err != nil || !ok || res.Job != "app" || res.Number != 1 || res.Replica != "r0" || res.ReservedAt.IsZero() {
		t.Fatalf("the run's reservation: %+v %v %v", res, ok, err)
	}
	// The lease moves to r1 (r0 died) and the run is dispatched again there.
	if _, err := s.Pool.Exec(ctx, `UPDATE home_job_owners SET expires_at = now() - interval '1 second' WHERE job = 'app'`); err != nil {
		t.Fatal(err)
	}
	b, err := s.ClaimJob(ctx, "app", "r1", time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	var taken *RunTakenError
	if err := s.ReserveRun(ctx, "app", 2, "r1", b.Epoch, runA); !errors.As(err, &taken) || taken.Job != "app" || taken.Number != 1 {
		t.Fatalf("a second number for the run: %v", err)
	}
	// Another job's lease cannot give it one either.
	c, err := s.ClaimJob(ctx, "other", "r1", time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	if err := s.ReserveRun(ctx, "other", 1, "r1", c.Epoch, runA); !errors.As(err, &taken) {
		t.Fatalf("the run given a number of another job: %v", err)
	}
	// Builds no run asked for are numbered as before.
	if err := s.ReserveRun(ctx, "app", 2, "r1", b.Epoch, ""); err != nil {
		t.Fatal(err)
	}
	// Released (its build never existed): the run may have a number again; #1 stays taken.
	if ok, err := s.ReleaseRun(ctx, runA, "app", 2); err != nil || ok {
		t.Fatalf("released under a number it does not have: %v %v", ok, err)
	}
	if ok, err := s.ReleaseRun(ctx, runA, "app", 1); err != nil || !ok {
		t.Fatalf("release: %v %v", ok, err)
	}
	if err := s.ReserveRun(ctx, "app", 3, "r1", b.Epoch, runA); err != nil {
		t.Fatalf("the released run: %v", err)
	}
	var nt *NumberTakenError
	if err := s.ReserveRun(ctx, "app", 1, "r1", b.Epoch, ""); !errors.As(err, &nt) {
		t.Fatalf("number 1 reused after the release: %v", err)
	}
}

// Two replicas giving one run a number at the same moment (two jobs' leases): one wins, the other
// is told where it went -- the unique index, not the check before it, decides a true race.
func TestTwoReservationsOfOneRunAtOnceGiveItOneNumber(t *testing.T) {
	s, ctx := testStore(t), context.Background()
	x, err := s.ClaimJob(ctx, "x", "r0", time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	y, err := s.ClaimJob(ctx, "y", "r1", time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	for i := 0; i < 20; i++ {
		run := fmt.Sprintf("00000000-0000-4000-8000-%012d", i)
		errs := make(chan error, 2)
		go func() { errs <- s.ReserveRun(ctx, "x", i+1, "r0", x.Epoch, run) }()
		go func() { errs <- s.ReserveRun(ctx, "y", i+1, "r1", y.Epoch, run) }()
		e1, e2 := <-errs, <-errs
		var taken *RunTakenError
		won := 0
		for _, e := range []error{e1, e2} {
			switch {
			case e == nil:
				won++
			case errors.As(e, &taken):
			default:
				t.Fatalf("round %d: %v", i, e)
			}
		}
		if won != 1 {
			t.Fatalf("round %d: %d reservations of one run (%v, %v)", i, won, e1, e2)
		}
	}
}
