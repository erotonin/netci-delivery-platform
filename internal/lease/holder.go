package lease

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"sync"
	"time"

	coordinationv1 "k8s.io/api/coordination/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	coordinationclient "k8s.io/client-go/kubernetes/typed/coordination/v1"
	"k8s.io/utils/clock"
)

// Holder keeps a cell's Lease for one controller pod and says when it may and may not act.
//
// The guarantee it gives: OnLost is called no later than RenewDeadline after the last renewal
// that reached the API server. RenewDeadline is shorter than the Lease duration, and anyone
// else -- another pod, the supervisor -- treats the Lease as expired only after seeing it
// unchanged for the full duration on its own clock. So by the time another party can act, the
// holder has already stopped, by the margin Duration - RenewDeadline.
type Holder struct {
	Leases    coordinationclient.LeasesGetter
	Namespace string
	Name      string
	// Identity is "<pod name>/<pod uid>": the replacement pod of a StatefulSet has the same name,
	// and must not mistake the old pod's Lease for its own.
	Identity      string
	Duration      time.Duration
	RenewInterval time.Duration
	RenewDeadline time.Duration
	Clock         clock.PassiveClock
	Observer      *Observer
	Log           *slog.Logger

	OnAcquired func(epoch int32)
	OnLost     func(reason string)

	mu        sync.Mutex // guards the three fields below, read by other goroutines
	holding   bool
	epoch     int32
	lastRenew time.Time
}

// ErrHeld means another identity holds a Lease that has not expired.
var ErrHeld = errors.New("lease held by another identity")

// ErrFenced means this identity was fenced: the supervisor powered its machine off and gave the
// Lease up on its behalf. It never holds the Lease again; only a new pod (a new identity) can.
var ErrFenced = errors.New("this identity was fenced")

// FencedAnnotation names the identity the supervisor fenced when it released the Lease.
const FencedAnnotation = "netci.io/fenced-identity"

// Validate checks the timing contract before anything runs on it.
func (h *Holder) Validate() error {
	switch {
	case h.Identity == "" || h.Name == "" || h.Namespace == "":
		return errors.New("lease holder needs a namespace, a name and an identity")
	case h.RenewInterval <= 0 || h.RenewDeadline <= h.RenewInterval:
		return fmt.Errorf("renew deadline (%s) must exceed the renew interval (%s)", h.RenewDeadline, h.RenewInterval)
	case h.Duration <= h.RenewDeadline:
		return fmt.Errorf("lease duration (%s) must exceed the renew deadline (%s): that margin is what stops two holders acting at once", h.Duration, h.RenewDeadline)
	case h.Duration%time.Second != 0:
		return fmt.Errorf("lease duration must be whole seconds, got %s", h.Duration)
	}
	return nil
}

// Holding reports whether this holder may act now, and with which epoch.
func (h *Holder) Holding() (bool, int32) {
	h.mu.Lock()
	defer h.mu.Unlock()
	return h.holding, h.epoch
}

// Run renews until ctx is done. It never returns an error: losing the Lease is reported through
// OnLost, and the loop keeps trying to get it back.
func (h *Holder) Run(ctx context.Context) {
	t := time.NewTicker(h.RenewInterval)
	defer t.Stop()
	for {
		h.Step(ctx)
		select {
		case <-ctx.Done():
			return
		case <-t.C:
		}
	}
}

// Step is one renewal attempt and the deadline check that goes with it.
func (h *Holder) Step(ctx context.Context) {
	now := h.Clock.Now()
	timeout := h.RenewInterval
	h.mu.Lock()
	holding, lastRenew, current := h.holding, h.lastRenew, h.epoch
	h.mu.Unlock()
	if holding {
		// A call that hangs must not carry the holder past its deadline.
		if left := h.RenewDeadline - now.Sub(lastRenew); left < timeout {
			timeout = left
		}
	}
	var err error
	var epoch int32
	if timeout > 0 {
		callCtx, cancel := context.WithTimeout(ctx, timeout)
		epoch, err = h.acquireOrRenew(callCtx)
		cancel()
	} else {
		err = context.DeadlineExceeded
	}
	now = h.Clock.Now()
	if err == nil {
		h.mu.Lock()
		h.lastRenew = now
		changed := !holding || epoch != current
		h.holding, h.epoch = true, epoch
		h.mu.Unlock()
		if changed {
			h.log().Info("lease acquired", "lease", h.Name, "epoch", epoch)
			if h.OnAcquired != nil {
				h.OnAcquired(epoch)
			}
		}
		return
	}
	if !holding {
		return
	}
	if errors.Is(err, ErrHeld) || errors.Is(err, ErrFenced) || now.Sub(lastRenew) >= h.RenewDeadline {
		h.mu.Lock()
		h.holding = false
		h.mu.Unlock()
		reason := fmt.Sprintf("not renewed for %s: %v", now.Sub(lastRenew).Round(time.Millisecond), err)
		if errors.Is(err, ErrHeld) || errors.Is(err, ErrFenced) {
			reason = err.Error()
		}
		h.log().Warn("lease lost", "lease", h.Name, "epoch", current, "reason", reason)
		if h.OnLost != nil {
			h.OnLost(reason)
		}
	}
}

func (h *Holder) acquireOrRenew(ctx context.Context) (int32, error) {
	leases := h.Leases.Leases(h.Namespace)
	now := metav1.NewMicroTime(h.Clock.Now())
	seconds := int32(h.Duration / time.Second)
	l, err := leases.Get(ctx, h.Name, metav1.GetOptions{})
	if apierrors.IsNotFound(err) {
		zero := int32(0)
		_, err = leases.Create(ctx, &coordinationv1.Lease{
			ObjectMeta: metav1.ObjectMeta{Name: h.Name, Namespace: h.Namespace},
			Spec: coordinationv1.LeaseSpec{
				HolderIdentity: &h.Identity, LeaseDurationSeconds: &seconds,
				AcquireTime: &now, RenewTime: &now, LeaseTransitions: &zero,
			},
		}, metav1.CreateOptions{})
		return 0, err
	}
	if err != nil {
		return 0, err
	}
	obs := h.Observer.Observe(l)
	if l.Annotations[FencedAnnotation] == h.Identity {
		return 0, ErrFenced
	}
	next := l.DeepCopy()
	next.Spec.RenewTime = &now
	next.Spec.LeaseDurationSeconds = &seconds
	epoch := obs.Epoch
	switch {
	case obs.Holder == h.Identity:
	case obs.Expired:
		epoch = obs.Epoch + 1
		next.Spec.HolderIdentity = &h.Identity
		next.Spec.AcquireTime = &now
		next.Spec.LeaseTransitions = &epoch
	default:
		return 0, fmt.Errorf("%w: %s (unchanged for %s of %s)", ErrHeld, obs.Holder, obs.SinceChange.Round(time.Millisecond), obs.Duration)
	}
	// The update carries the resourceVersion read above: a concurrent writer makes it fail with
	// a conflict, and nobody is told they hold a Lease they lost the race for.
	updated, err := leases.Update(ctx, next, metav1.UpdateOptions{})
	if err != nil {
		return 0, err
	}
	h.Observer.Observe(updated)
	return epoch, nil
}

// Release gives the Lease up so a successor need not wait for it to expire. Call it only once
// the controller this holder guards has stopped.
// It must not run while Run or Step is still renewing: stop the renewal loop first, or the next
// renewal would see the Lease free and take it back.
func (h *Holder) Release(ctx context.Context) error {
	if holding, _ := h.Holding(); !holding {
		return nil
	}
	leases := h.Leases.Leases(h.Namespace)
	l, err := leases.Get(ctx, h.Name, metav1.GetOptions{})
	if err != nil {
		return err
	}
	if l.Spec.HolderIdentity == nil || *l.Spec.HolderIdentity != h.Identity {
		h.mu.Lock()
		h.holding = false
		h.mu.Unlock()
		return nil
	}
	next := l.DeepCopy()
	next.Spec.HolderIdentity = nil
	if _, err := leases.Update(ctx, next, metav1.UpdateOptions{}); err != nil {
		return err
	}
	h.mu.Lock()
	h.holding = false
	epoch := h.epoch
	h.mu.Unlock()
	h.log().Info("lease released", "lease", h.Name, "epoch", epoch)
	return nil
}

func (h *Holder) log() *slog.Logger {
	if h.Log != nil {
		return h.Log
	}
	return slog.Default()
}

// ReleaseFenced gives the Lease up on behalf of a holder whose machine has been confirmed off,
// so its successor need not wait out the duration. l must be the copy the caller observed as
// expired: the update carries its resourceVersion, so if the holder renewed in the meantime the
// release fails with a conflict instead of taking a live Lease away.
func ReleaseFenced(ctx context.Context, leases coordinationclient.LeasesGetter, l *coordinationv1.Lease) (*coordinationv1.Lease, error) {
	if l.Spec.HolderIdentity == nil || *l.Spec.HolderIdentity == "" {
		return l, nil
	}
	next := l.DeepCopy()
	if next.Annotations == nil {
		next.Annotations = map[string]string{}
	}
	next.Annotations[FencedAnnotation] = *l.Spec.HolderIdentity
	next.Spec.HolderIdentity = nil
	return leases.Leases(l.Namespace).Update(ctx, next, metav1.UpdateOptions{})
}
