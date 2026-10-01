package lease

import (
	"context"
	"errors"
	"strings"
	"testing"
	"time"

	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	clocktesting "k8s.io/utils/clock/testing"

	"github.com/erotonin/netci-delivery-platform/internal/lease/leasetest"
)

const (
	duration = 6 * time.Second
	interval = 1 * time.Second
	deadline = 4 * time.Second
)

type events struct {
	acquired []int32
	lost     []string
	lostAt   []time.Time
}

func holder(api *leasetest.APIServer, clk *clocktesting.FakeClock, identity string) (*Holder, *events) {
	ev := &events{}
	h := &Holder{
		Leases: api, Namespace: "cell-a", Name: "netci-cell", Identity: identity,
		Duration: duration, RenewInterval: interval, RenewDeadline: deadline,
		Clock: clk, Observer: NewObserver(clk),
	}
	h.OnAcquired = func(e int32) { ev.acquired = append(ev.acquired, e) }
	h.OnLost = func(r string) { ev.lost = append(ev.lost, r); ev.lostAt = append(ev.lostAt, clk.Now()) }
	return h, ev
}

// run steps every holder once per interval for n intervals.
func run(ctx context.Context, clk *clocktesting.FakeClock, n int, hs ...*Holder) {
	for i := 0; i < n; i++ {
		for _, h := range hs {
			h.Step(ctx)
		}
		clk.Step(interval)
	}
}

func TestValidateRejectsTimingsWithoutASafetyMargin(t *testing.T) {
	base := func() *Holder {
		return &Holder{Namespace: "n", Name: "l", Identity: "p/u", Duration: 6 * time.Second, RenewInterval: time.Second, RenewDeadline: 4 * time.Second}
	}
	if err := base().Validate(); err != nil {
		t.Fatalf("valid timings rejected: %v", err)
	}
	for i, mutate := range []func(*Holder){
		func(h *Holder) { h.RenewDeadline = 6 * time.Second },    // no margin before others may act
		func(h *Holder) { h.RenewDeadline = time.Second },        // not longer than one interval
		func(h *Holder) { h.Duration = 5500 * time.Millisecond }, // not whole seconds
		func(h *Holder) { h.Identity = "" },
	} {
		h := base()
		mutate(h)
		if h.Validate() == nil {
			t.Fatalf("case %d accepted", i)
		}
	}
}

func TestObserverCallsALeaseExpiredOnlyAfterSeeingItUnchangedForItsDuration(t *testing.T) {
	api, clk := leasetest.New(), clocktesting.NewFakeClock(time.Unix(1000, 0))
	h, _ := holder(api, clk, "jenkins-0/a")
	h.Step(context.Background())
	o := NewObserver(clk)
	l := api.Get("cell-a", "netci-cell")
	if obs := o.Observe(l); obs.Expired || obs.Holder != "jenkins-0/a" {
		t.Fatalf("first sight must not be expired: %+v", obs)
	}
	clk.Step(duration - time.Millisecond)
	if o.Observe(l).Expired {
		t.Fatal("expired before a full duration")
	}
	clk.Step(time.Millisecond)
	if !o.Observe(l).Expired {
		t.Fatal("not expired after a full duration unchanged")
	}
	// Any write is a change. (h itself would refuse to renew now: it is past its own deadline.)
	if _, err := api.Leases("cell-a").Update(context.Background(), l, metav1.UpdateOptions{}); err != nil {
		t.Fatal(err)
	}
	if o.Observe(api.Get("cell-a", "netci-cell")).Expired {
		t.Fatal("a lease that changed must not read as expired")
	}
}

func TestObserverIgnoresTheHoldersClock(t *testing.T) {
	// The holder writes renewTime from a clock an hour behind: an observer that compared
	// renewTime with its own clock would call a live holder expired.
	api := leasetest.New()
	holderClock := clocktesting.NewFakeClock(time.Unix(1000, 0))
	h, _ := holder(api, holderClock, "jenkins-0/a")
	h.Step(context.Background())
	observerClock := clocktesting.NewFakeClock(time.Unix(1000, 0).Add(time.Hour))
	o := NewObserver(observerClock)
	for i := 0; i < 10; i++ {
		if o.Observe(api.Get("cell-a", "netci-cell")).Expired {
			t.Fatalf("live holder called expired at renewal %d", i)
		}
		holderClock.Step(interval)
		observerClock.Step(interval)
		h.Step(context.Background())
	}
}

func TestAHolderCreatesTheLeaseAndKeepsItsEpoch(t *testing.T) {
	api, clk := leasetest.New(), clocktesting.NewFakeClock(time.Unix(1000, 0))
	h, ev := holder(api, clk, "jenkins-0/a")
	run(context.Background(), clk, 10, h)
	if len(ev.acquired) != 1 || ev.acquired[0] != 0 || len(ev.lost) != 0 {
		t.Fatalf("acquired=%v lost=%v", ev.acquired, ev.lost)
	}
	if ok, epoch := h.Holding(); !ok || epoch != 0 {
		t.Fatalf("holding=%v epoch=%d", ok, epoch)
	}
}

func TestNobodyTakesALeaseThatIsBeingRenewed(t *testing.T) {
	api, clk := leasetest.New(), clocktesting.NewFakeClock(time.Unix(1000, 0))
	a, _ := holder(api, clk, "jenkins-0/a")
	b, evB := holder(api, clk, "jenkins-0/b")
	run(context.Background(), clk, 60, a, b)
	if len(evB.acquired) != 0 {
		t.Fatalf("b acquired a lease a kept renewing: %v", evB.acquired)
	}
}

func TestTheHolderStopsBeforeAnyoneElseCanStart(t *testing.T) {
	// The API server becomes unreachable for a (a partition): a must report the loss within its
	// renew deadline, and b -- which can reach the server -- may only acquire after seeing the
	// lease unchanged for the whole duration. The gap between the two is the safety margin.
	api, clk := leasetest.New(), clocktesting.NewFakeClock(time.Unix(1000, 0))
	a, evA := holder(api, clk, "jenkins-0/a")
	b, evB := holder(api, clk, "jenkins-0/b")
	ctx := context.Background()
	run(ctx, clk, 5, a, b)
	lastRenew := clk.Now().Add(-interval)

	a.Leases = leasetest.Unreachable{}
	var bAcquiredAt time.Time
	for i := 0; i < 100 && bAcquiredAt.IsZero(); i++ {
		a.Step(ctx)
		b.Step(ctx)
		if len(evB.acquired) > 0 {
			bAcquiredAt = clk.Now()
		}
		clk.Step(200 * time.Millisecond)
	}
	if len(evA.lost) != 1 {
		t.Fatalf("a never reported losing the lease: %v", evA.lost)
	}
	if got := evA.lostAt[0].Sub(lastRenew); got > deadline+200*time.Millisecond {
		t.Fatalf("a stopped %s after its last renewal; the contract is %s", got, deadline)
	}
	if bAcquiredAt.IsZero() {
		t.Fatal("b never acquired an abandoned lease")
	}
	if margin := bAcquiredAt.Sub(evA.lostAt[0]); margin < duration-deadline-time.Second {
		t.Fatalf("b started only %s after a stopped; expected about %s", margin, duration-deadline)
	}
	if evB.acquired[0] != 1 {
		t.Fatalf("a takeover must bump the epoch: got %d", evB.acquired[0])
	}
}

func TestTwoSuccessorsRacingForAnExpiredLeaseCannotBothWin(t *testing.T) {
	api, clk := leasetest.New(), clocktesting.NewFakeClock(time.Unix(1000, 0))
	a, _ := holder(api, clk, "jenkins-0/a")
	a.Step(context.Background())
	b, evB := holder(api, clk, "jenkins-0/b")
	c, evC := holder(api, clk, "jenkins-0/c")
	b.Step(context.Background())
	c.Step(context.Background())
	clk.Step(duration)
	// Both read the same expired lease before either writes.
	lb, lc := api.Get("cell-a", "netci-cell"), api.Get("cell-a", "netci-cell")
	b.Observer.Observe(lb)
	c.Observer.Observe(lc)
	_, errB := b.acquireOrRenew(context.Background())
	_, errC := c.acquireOrRenew(context.Background())
	if (errB == nil) == (errC == nil) {
		t.Fatalf("exactly one must win: b=%v c=%v", errB, errC)
	}
	loser := errB
	if errB == nil {
		loser = errC
	}
	if !apierrors.IsConflict(loser) && !errors.Is(loser, ErrHeld) {
		t.Fatalf("the loser must see a conflict or a held lease, got %v", loser)
	}
	_ = evB
	_ = evC
}

func TestAFencedIdentityNeverHoldsTheLeaseAgain(t *testing.T) {
	api, clk := leasetest.New(), clocktesting.NewFakeClock(time.Unix(1000, 0))
	old, evOld := holder(api, clk, "jenkins-0/old")
	ctx := context.Background()
	run(ctx, clk, 3, old)
	// The supervisor saw the lease expire (old's machine lost power), confirmed the machine off
	// and released the lease on its behalf.
	observed := api.Get("cell-a", "netci-cell")
	if _, err := ReleaseFenced(ctx, api, observed); err != nil {
		t.Fatal(err)
	}
	// The machine comes back (someone powers it on) and old's agent tries again.
	run(ctx, clk, 10, old)
	if len(evOld.lost) != 1 || !strings.Contains(evOld.lost[0], "fenced") {
		t.Fatalf("old must learn it was fenced: %v", evOld.lost)
	}
	if ok, _ := old.Holding(); ok {
		t.Fatal("a fenced identity holds the lease again")
	}
	successor, evNew := holder(api, clk, "jenkins-0/new")
	successor.Step(ctx)
	if len(evNew.acquired) != 1 || evNew.acquired[0] != 1 {
		t.Fatalf("the replacement pod must acquire at once with a new epoch: %v", evNew.acquired)
	}
}

func TestReleasingOnBehalfOfAHolderThatRenewedMeanwhileFails(t *testing.T) {
	api, clk := leasetest.New(), clocktesting.NewFakeClock(time.Unix(1000, 0))
	h, _ := holder(api, clk, "jenkins-0/a")
	h.Step(context.Background())
	stale := api.Get("cell-a", "netci-cell")
	clk.Step(interval)
	h.Step(context.Background()) // renewed after the supervisor read it
	if _, err := ReleaseFenced(context.Background(), api, stale); !apierrors.IsConflict(err) {
		t.Fatalf("a release based on a stale read must conflict, got %v", err)
	}
	if l := api.Get("cell-a", "netci-cell"); l.Spec.HolderIdentity == nil || *l.Spec.HolderIdentity != "jenkins-0/a" {
		t.Fatal("the live holder lost its lease")
	}
}

func TestAGracefulReleaseLetsTheSuccessorStartAtOnce(t *testing.T) {
	api, clk := leasetest.New(), clocktesting.NewFakeClock(time.Unix(1000, 0))
	a, _ := holder(api, clk, "jenkins-0/a")
	a.Step(context.Background())
	if err := a.Release(context.Background()); err != nil {
		t.Fatal(err)
	}
	b, evB := holder(api, clk, "jenkins-0/b")
	b.Step(context.Background())
	if len(evB.acquired) != 1 {
		t.Fatal("successor should not have to wait after a graceful release")
	}
}

func TestATransientOutageShorterThanTheDeadlineIsNotALoss(t *testing.T) {
	api, clk := leasetest.New(), clocktesting.NewFakeClock(time.Unix(1000, 0))
	h, ev := holder(api, clk, "jenkins-0/a")
	ctx := context.Background()
	run(ctx, clk, 3, h)
	api.SetUnreachable(true)
	// Two failed attempts: the next one is still inside the deadline. (A third would put the
	// next attempt exactly at the deadline, which is a loss by contract.)
	run(ctx, clk, int(deadline/interval)-2, h)
	api.SetUnreachable(false)
	run(ctx, clk, 3, h)
	if len(ev.lost) != 0 || len(ev.acquired) != 1 {
		t.Fatalf("lost=%v acquired=%v", ev.lost, ev.acquired)
	}
}
