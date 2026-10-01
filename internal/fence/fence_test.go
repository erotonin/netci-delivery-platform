package fence

import (
	"context"
	"errors"
	"sync"
	"testing"
	"time"
)

type fakeFencer struct {
	mu        sync.Mutex
	state     State
	stateErr  error
	offCalls  int
	staysOn   bool // PowerOff "succeeds" but the machine keeps running
	powerErr  error
	offAfterN int // State reports Off only after this many calls following PowerOff
	calls     int
}

func (f *fakeFencer) Name() string { return "fake" }
func (f *fakeFencer) State(context.Context, string) (State, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.calls++
	if f.stateErr != nil {
		return Unknown, f.stateErr
	}
	if f.offCalls > 0 && !f.staysOn && f.calls > f.offAfterN {
		return Off, nil
	}
	return f.state, nil
}
func (f *fakeFencer) PowerOff(context.Context, string) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.powerErr != nil {
		return f.powerErr
	}
	f.offCalls++
	f.calls = 0
	return nil
}
func (f *fakeFencer) PowerOn(context.Context, string) error { return nil }

func TestEnsureOffDoesNothingToAMachineAlreadyOff(t *testing.T) {
	f := &fakeFencer{state: Off}
	powered, err := EnsureOff(context.Background(), f, "m", time.Second)
	if err != nil || powered || f.offCalls != 0 {
		t.Fatalf("powered=%v err=%v offCalls=%d", powered, err, f.offCalls)
	}
}

func TestEnsureOffWaitsUntilTheMachineReportsOff(t *testing.T) {
	f := &fakeFencer{state: Running, offAfterN: 3}
	powered, err := EnsureOff(context.Background(), f, "m", 5*time.Second)
	if err != nil || !powered || f.offCalls != 1 {
		t.Fatalf("powered=%v err=%v offCalls=%d", powered, err, f.offCalls)
	}
}

func TestEnsureOffFailsWhenTheMachineNeverReportsOff(t *testing.T) {
	f := &fakeFencer{state: Running, staysOn: true}
	_, err := EnsureOff(context.Background(), f, "m", 600*time.Millisecond)
	if !errors.Is(err, ErrNotOff) {
		t.Fatalf("want ErrNotOff, got %v", err)
	}
}

func TestEnsureOffNeverTreatsUnknownAsOff(t *testing.T) {
	f := &fakeFencer{stateErr: errors.New("agent unreachable")}
	_, err := EnsureOff(context.Background(), f, "m", 600*time.Millisecond)
	if err == nil {
		t.Fatal("an unreachable power agent must not read as a machine that is off")
	}
}

func TestEnsureOffReportsAPowerOffThatFailed(t *testing.T) {
	f := &fakeFencer{state: Running, powerErr: errors.New("denied")}
	if _, err := EnsureOff(context.Background(), f, "m", time.Second); err == nil {
		t.Fatal("expected the power-off error")
	}
}
