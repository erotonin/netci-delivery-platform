package cellagent

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"os"
	"os/exec"
	"syscall"
	"time"
)

// Guard runs the controller inside its own container, as that container's command:
//
//	cell-agent guard -- /usr/local/bin/jenkins.sh
//
// It starts the controller only once the gate is open, and kills it when the gate closes or
// goes stale. The agent refreshes the gate's mtime to the start of every successful renewal, so
// a stale gate means the agent has stopped renewing for whatever reason -- including the agent
// itself having crashed or hung, which the agent's own kill cannot cover.
//
// The gate's mtime is wall-clock time shared by both containers on the node. A backward step
// of the node's clock would make the gate look fresher than it is; the agent's own deadline,
// on its monotonic clock, still applies, and this guard is the second line, not the first.
type Guard struct {
	GateFile string
	// Stale: the controller is killed once the last renewal started this long ago. It must not
	// exceed the agent's renew deadline.
	Stale   time.Duration
	Poll    time.Duration
	Command []string
	Log     *slog.Logger
	Now     func() time.Time
	// BeforeStart, if set, runs once the gate is open and before the command: what must write
	// JENKINS_HOME before the controller does, under the Lease. An error is logged; the
	// controller still starts.
	BeforeStart func() error
}

// ErrGateClosed: the controller was killed because the gate closed or went stale.
var ErrGateClosed = errors.New("the gate closed: the controller was killed")

// Run waits for the gate, runs the command and watches the gate until the command exits. It
// returns the command's exit code. SIGTERM and SIGINT received by this process are passed on.
func (g *Guard) Run(ctx context.Context, signals <-chan os.Signal) (int, error) {
	if len(g.Command) == 0 {
		return 2, errors.New("no command to guard")
	}
	t := time.NewTicker(g.Poll)
	defer t.Stop()
	for !g.open() {
		select {
		case <-ctx.Done():
			return 0, nil
		case <-signals:
			return 0, nil // terminated before it was ever allowed to start
		case <-t.C:
		}
	}
	if g.BeforeStart != nil {
		if err := g.BeforeStart(); err != nil {
			g.Log.Warn("before starting the controller", "error", err)
		}
	}
	cmd := exec.Command(g.Command[0], g.Command[1:]...)
	cmd.Stdin, cmd.Stdout, cmd.Stderr = os.Stdin, os.Stdout, os.Stderr
	// Its own process group, so a kill reaches whatever the start script left running too.
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	if err := cmd.Start(); err != nil {
		return 127, err
	}
	g.Log.Info("gate open: controller started", "pid", cmd.Process.Pid)
	exited := make(chan error, 1)
	go func() { exited <- cmd.Wait() }()

	for {
		select {
		case err := <-exited:
			return exitCode(err), nil
		case sig := <-signals:
			g.Log.Info("passing the signal on to the controller", "signal", sig.String())
			_ = cmd.Process.Signal(sig)
		case <-t.C:
			if g.open() {
				continue
			}
			_ = syscall.Kill(-cmd.Process.Pid, syscall.SIGKILL)
			<-exited
			g.Log.Warn("gate closed or stale: controller killed")
			return 137, ErrGateClosed
		}
	}
}

// open reports whether the gate exists and was refreshed within Stale.
func (g *Guard) open() bool {
	fi, err := os.Stat(g.GateFile)
	if err != nil {
		return false
	}
	return g.Now().Sub(fi.ModTime()) < g.Stale
}

func exitCode(err error) int {
	if err == nil {
		return 0
	}
	var ee *exec.ExitError
	if errors.As(err, &ee) {
		if ws, ok := ee.Sys().(syscall.WaitStatus); ok && ws.Signaled() {
			return 128 + int(ws.Signal())
		}
		return ee.ExitCode()
	}
	return 1
}

// refreshGate sets the gate's mtime to when the last renewal started.
func refreshGate(path string, started time.Time) error {
	if err := os.Chtimes(path, started, started); err != nil {
		return fmt.Errorf("refresh gate: %w", err)
	}
	return nil
}
