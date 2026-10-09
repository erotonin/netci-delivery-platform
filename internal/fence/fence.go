// Package fence powers machines off and reports whether they are off.
//
// A controller may only be started elsewhere once the machine that ran it can no longer write
// (ADR-060). Kubernetes' out-of-service taint detaches a node's volumes, but its own rule is
// that the node must already be shut down; this package is how that is established instead of
// assumed. A Fencer that cannot tell answers Unknown, and Unknown never counts as Off.
package fence

import (
	"context"
	"errors"
	"fmt"
	"time"
)

// State of a machine as its power controller reports it.
type State string

const (
	Running State = "running"
	Off     State = "off"
	Unknown State = "unknown"
)

// Fencer controls the power of the machines that run cells.
type Fencer interface {
	// Name identifies the implementation in logs and events.
	Name() string
	// State reports the machine's power state. Any doubt is Unknown.
	State(ctx context.Context, machine string) (State, error)
	// PowerOff stops the machine immediately (a hard stop, not a shutdown request).
	PowerOff(ctx context.Context, machine string) error
	// PowerOn starts a machine that is off.
	PowerOn(ctx context.Context, machine string) error
}

// ErrNotOff is returned when a machine was powered off but never reported Off.
var ErrNotOff = errors.New("machine did not report off")

// Refusal is a power controller's definite no -- the request is wrong or not allowed, and
// sending it again will not change the answer (a Redfish 400, 401, 403, 404 or 405).
type Refusal interface{ Refused() bool }

// retryPowerOffEvery: how soon a power-off that failed without a refusal is sent again.
const retryPowerOffEvery = 2 * time.Second

// EnsureOff powers the machine off if needed and returns only once the fencer reports Off.
// It is the single entry point the supervisor uses, so "fenced" always means "seen off".
//
// A power-off that fails is not yet an answer. Real BMCs fail ones that worked or will work:
// iDRAC answers 409 "already powered OFF" when the machine lost its power between the state read
// and the request, and refuses for a while right after a power change; a slow BMC can carry out
// a request whose answer arrived after the client gave up. So until the deadline it keeps asking
// for the state, and sends the power-off again every few seconds -- a hard stop sent twice stops
// the machine once. Only a refusal ends it early. In every case the machine counts as off only
// once the power controller says Off.
func EnsureOff(ctx context.Context, f Fencer, machine string, timeout time.Duration) (powered bool, err error) {
	state, err := f.State(ctx, machine)
	if err != nil {
		return false, fmt.Errorf("state of %s: %w", machine, err)
	}
	if state == Off {
		return false, nil
	}
	deadline := time.Now().Add(timeout)
	offErr := f.PowerOff(ctx, machine)
	if refused(offErr) {
		return false, fmt.Errorf("power off %s: %w", machine, offErr)
	}
	lastTry := time.Now()
	for {
		state, err := f.State(ctx, machine)
		if err == nil && state == Off {
			return offErr == nil, nil
		}
		if time.Now().After(deadline) {
			if offErr != nil {
				return false, fmt.Errorf("power off %s: %w; %w (last state %q, error %v)", machine, offErr, ErrNotOff, state, err)
			}
			return true, fmt.Errorf("%s: %w (last state %q, error %v)", machine, ErrNotOff, state, err)
		}
		if offErr != nil && err == nil && state == Running && time.Since(lastTry) >= retryPowerOffEvery {
			offErr, lastTry = f.PowerOff(ctx, machine), time.Now()
			if refused(offErr) {
				return false, fmt.Errorf("power off %s: %w", machine, offErr)
			}
		}
		select {
		case <-ctx.Done():
			return offErr == nil, ctx.Err()
		case <-time.After(250 * time.Millisecond):
		}
	}
}

func refused(err error) bool {
	var r Refusal
	return errors.As(err, &r) && r.Refused()
}
