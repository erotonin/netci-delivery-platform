// Package lease holds and watches the Kubernetes Lease that says which controller pod may run
// a cell (ADR-060).
//
// Expiry is never computed from the times written in the Lease. Those come from the holder's
// clock, and a skewed clock would make a live holder look expired. A Lease is expired for an
// observer when the observer has seen it unchanged for its whole duration, measured on the
// observer's own monotonic clock -- the rule client-go's leader election uses, kept here because
// the cell agent and the supervisor both need it and must agree.
package lease

import (
	"sync"
	"time"

	coordinationv1 "k8s.io/api/coordination/v1"
	"k8s.io/utils/clock"
)

// Observation of one Lease at one moment.
type Observation struct {
	Holder      string
	Epoch       int32         // spec.leaseTransitions: increases every time the holder changes
	Duration    time.Duration // spec.leaseDurationSeconds
	SinceChange time.Duration // how long this observer has seen it unchanged
	// Expired: unchanged for at least Duration on this observer's clock, or never held.
	Expired bool
}

// Observer remembers when it last saw each Lease change.
type Observer struct {
	Clock clock.PassiveClock
	mu    sync.Mutex
	seen  map[string]seen
}

type seen struct {
	resourceVersion string
	changedAt       time.Time
}

// NewObserver returns an observer on the real clock unless one is given.
func NewObserver(c clock.PassiveClock) *Observer {
	if c == nil {
		c = clock.RealClock{}
	}
	return &Observer{Clock: c, seen: map[string]seen{}}
}

// Observe records the Lease and reports it. The first observation of a Lease counts as a
// change: an observer that has just started cannot know how long it has been unchanged, and
// must wait a full duration before it can call it expired.
func (o *Observer) Observe(l *coordinationv1.Lease) Observation {
	now := o.Clock.Now()
	key := l.Namespace + "/" + l.Name
	o.mu.Lock()
	s, ok := o.seen[key]
	if !ok || s.resourceVersion != l.ResourceVersion {
		s = seen{resourceVersion: l.ResourceVersion, changedAt: now}
		o.seen[key] = s
	}
	o.mu.Unlock()

	obs := Observation{SinceChange: now.Sub(s.changedAt)}
	if l.Spec.HolderIdentity != nil {
		obs.Holder = *l.Spec.HolderIdentity
	}
	if l.Spec.LeaseTransitions != nil {
		obs.Epoch = *l.Spec.LeaseTransitions
	}
	if l.Spec.LeaseDurationSeconds != nil {
		obs.Duration = time.Duration(*l.Spec.LeaseDurationSeconds) * time.Second
	}
	obs.Expired = obs.Holder == "" || (obs.Duration > 0 && obs.SinceChange >= obs.Duration)
	return obs
}

// Forget drops what is known about a Lease (it was deleted).
func (o *Observer) Forget(namespace, name string) {
	o.mu.Lock()
	delete(o.seen, namespace+"/"+name)
	o.mu.Unlock()
}

// EpochOf returns a Lease's epoch (spec.leaseTransitions), 0 if unset.
func EpochOf(l *coordinationv1.Lease) int32 {
	if l == nil || l.Spec.LeaseTransitions == nil {
		return 0
	}
	return *l.Spec.LeaseTransitions
}
