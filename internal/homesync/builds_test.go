package homesync

import (
	"context"
	"errors"
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
