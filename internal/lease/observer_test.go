package lease

import (
	"testing"
	"time"

	coordinationv1 "k8s.io/api/coordination/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	testingclock "k8s.io/utils/clock/testing"
)

func renewedAt(rv string, at time.Time) *coordinationv1.Lease {
	t := metav1.NewMicroTime(at)
	return &coordinationv1.Lease{ObjectMeta: metav1.ObjectMeta{Namespace: "cell", Name: "jenkins", ResourceVersion: rv},
		Spec: coordinationv1.LeaseSpec{RenewTime: &t}}
}

// A version first seen after a gap was written during the gap; the holder's renewTime places
// it, within what the observer can know: after its last look, and not after now.
func TestAChangeFirstSeenAfterAGapIsPlacedWithinTheGap(t *testing.T) {
	now := time.Unix(10000, 0)
	lastLook := now.Add(-6 * time.Second)
	for name, c := range map[string]struct {
		renewed time.Time
		want    time.Duration
	}{
		"renewed during the gap":                   {now.Add(-5 * time.Second), 5 * time.Second},
		"a holder clock behind: not before the gap": {now.Add(-60 * time.Second), 6 * time.Second},
		"a holder clock ahead: not after now":       {now.Add(30 * time.Second), 0},
	} {
		o := NewObserver(testingclock.NewFakePassiveClock(now))
		if got := o.ObserveSince(renewedAt("7", c.renewed), lastLook).SinceChange; got != c.want {
			t.Errorf("%s: %s, want %s", name, got, c.want)
		}
	}
	o := NewObserver(testingclock.NewFakePassiveClock(now))
	if got := o.ObserveSince(renewedAt("7", now.Add(-5*time.Second)), time.Time{}).SinceChange; got != 0 {
		t.Errorf("with no earlier look the change counts as now, got %s", got)
	}
}

func TestAVersionAlreadySeenKeepsItsTime(t *testing.T) {
	clk := testingclock.NewFakePassiveClock(time.Unix(10000, 0))
	o := NewObserver(clk)
	o.ObserveSince(renewedAt("7", clk.Now()), clk.Now().Add(-time.Second))
	clk.SetTime(clk.Now().Add(4 * time.Second))
	if got := o.ObserveSince(renewedAt("7", clk.Now()), clk.Now().Add(-time.Second)).SinceChange; got != 4*time.Second {
		t.Fatalf("%s: the same version must not move its change time", got)
	}
}
