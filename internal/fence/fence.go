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

// EnsureOff powers the machine off if needed and returns only once the fencer reports Off.
// It is the single entry point the supervisor uses, so "fenced" always means "seen off".
func EnsureOff(ctx context.Context, f Fencer, machine string, timeout time.Duration) (powered bool, err error) {
	state, err := f.State(ctx, machine)
	if err != nil {
		return false, fmt.Errorf("state of %s: %w", machine, err)
	}
	if state == Off {
		return false, nil
	}
	if err := f.PowerOff(ctx, machine); err != nil {
		return false, fmt.Errorf("power off %s: %w", machine, err)
	}
	deadline := time.Now().Add(timeout)
	for {
		state, err := f.State(ctx, machine)
		if err == nil && state == Off {
			return true, nil
		}
		if time.Now().After(deadline) {
			return true, fmt.Errorf("%s: %w (last state %q, error %v)", machine, ErrNotOff, state, err)
		}
		select {
		case <-ctx.Done():
			return true, ctx.Err()
		case <-time.After(250 * time.Millisecond):
		}
	}
}
