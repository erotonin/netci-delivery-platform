// Package cellagent is the sidecar that runs next to a cell's Jenkins controller (ADR-060).
//
// It holds the cell's Lease and turns holding it into the only way Jenkins can run:
//   - the controller container runs Jenkins under Guard, which waits for the gate file before
//     it starts Jenkins, so a container restart is gated too;
//   - every successful renewal sets the gate's mtime to when that renewal started; Guard kills
//     Jenkins once the gate is older than the renew deadline, which covers this agent itself
//     crashing or hanging;
//   - when the Lease is lost, the gate is removed and Jenkins is killed at once, and kept dead
//     for as long as the Lease is not held -- a controller that is cut off from the cluster
//     stops writing before anyone else may start;
//   - on pod termination the Lease is kept until Jenkins has exited, then given up, so the
//     replacement pod does not wait out the Lease duration.
//
// The pod must share its process namespace (spec.shareProcessNamespace) and this container must
// run as the controller's user, so that it may signal the controller's processes.
package cellagent

import (
	"context"
	"errors"
	"fmt"
	"io/fs"
	"log/slog"
	"os"
	"path/filepath"
	"time"

	"github.com/erotonin/netci-delivery-platform/internal/lease"
)

// Agent ties a lease.Holder to the gate file and the controller's processes.
type Agent struct {
	Holder   *lease.Holder
	GateFile string
	Procs    *Processes
	Log      *slog.Logger
	// KeepDeadEvery is how often, while not holding, the agent checks that Jenkins is not
	// running (a container restart would otherwise start it).
	KeepDeadEvery time.Duration
	Metrics       *Metrics

	renewCtx     context.Context
	stopRenewing context.CancelFunc
	renewing     chan struct{}
}

// Wire installs the agent's reactions on the holder and prepares the renewal loop. Call once,
// before Run, from the goroutine that will later call Shutdown.
func (a *Agent) Wire() {
	a.renewCtx, a.stopRenewing = context.WithCancel(context.Background())
	a.renewing = make(chan struct{})
	a.Holder.OnAcquired = func(epoch int32) {
		if err := a.openGate(epoch); err != nil {
			a.Log.Error("cannot open the gate; the controller stays stopped", "error", err)
			return
		}
		a.Metrics.setHolding(true, epoch)
		a.Log.Info("gate open: the controller may run", "epoch", epoch)
	}
	a.Holder.OnRenewed = func(started time.Time) {
		if err := refreshGate(a.GateFile, started); err != nil && !errors.Is(err, fs.ErrNotExist) {
			a.Log.Error("cannot refresh the gate; the guard will stop the controller", "error", err)
		}
	}
	a.Holder.OnLost = func(reason string) {
		a.closeGate()
		killed := a.Procs.KillJenkins()
		a.Metrics.setHolding(false, 0)
		a.Metrics.lost.Inc()
		a.Log.Warn("gate closed and controller killed: the lease is no longer held", "reason", reason, "killed", killed)
	}
}

// Run renews the lease and keeps the controller dead while it is not held, until ctx ends or
// Shutdown stops it.
//
// The renewal loop does not stop with ctx: on pod termination the lease must outlive the
// controller, and only Shutdown ends it.
func (a *Agent) Run(ctx context.Context) {
	a.closeGate() // nothing runs until the lease is held, whatever a previous container left
	go func() {
		defer close(a.renewing)
		a.Holder.Run(a.renewCtx)
	}()
	t := time.NewTicker(a.KeepDeadEvery)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-t.C:
			if holding, _ := a.Holder.Holding(); !holding {
				if killed := a.Procs.KillJenkins(); killed > 0 {
					a.Log.Warn("a controller ran without the lease and was killed", "killed", killed)
				}
			}
		}
	}
}

// Shutdown keeps the lease until Jenkins has exited (or the grace period ends), then releases
// it. The kubelet sends SIGTERM to every container at once; releasing first would let the
// replacement start while this controller is still writing.
func (a *Agent) Shutdown(ctx context.Context, grace time.Duration) {
	deadline := time.Now().Add(grace)
	for a.Procs.JenkinsRunning() && time.Now().Before(deadline) {
		time.Sleep(250 * time.Millisecond)
	}
	if a.Procs.JenkinsRunning() {
		a.Log.Warn("controller still running at the end of the grace period: not releasing the lease")
		a.closeGate()
		a.Procs.KillJenkins()
		return
	}
	a.closeGate()
	// Stop renewing before releasing: a renewal after the release would take the lease back.
	a.stopRenewing()
	<-a.renewing
	if err := a.Holder.Release(ctx); err != nil {
		a.Log.Warn("could not release the lease; the successor will wait for it to expire", "error", err)
	}
}

func (a *Agent) openGate(epoch int32) error {
	content := fmt.Sprintf("epoch=%d\nholder=%s\n", epoch, a.Holder.Identity)
	tmp := a.GateFile + ".tmp"
	if err := os.MkdirAll(filepath.Dir(a.GateFile), 0o755); err != nil {
		return err
	}
	if err := os.WriteFile(tmp, []byte(content), 0o644); err != nil {
		return err
	}
	return os.Rename(tmp, a.GateFile)
}

func (a *Agent) closeGate() {
	if err := os.Remove(a.GateFile); err != nil && !os.IsNotExist(err) {
		a.Log.Error("cannot remove the gate file", "error", err)
	}
}
