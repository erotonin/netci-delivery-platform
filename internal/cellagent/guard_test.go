package cellagent

import (
	"context"
	"errors"
	"io"
	"log/slog"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"testing"
	"time"
)

type guardRun struct {
	code int
	err  error
}

func startGuard(t *testing.T, gate string, script string) (chan os.Signal, chan guardRun) {
	t.Helper()
	g := &Guard{GateFile: gate, Stale: 600 * time.Millisecond, Poll: 50 * time.Millisecond,
		Command: []string{"/bin/sh", "-c", script}, Now: time.Now,
		Log: slog.New(slog.NewTextHandler(io.Discard, nil))}
	signals := make(chan os.Signal, 1)
	done := make(chan guardRun, 1)
	ctx, cancel := context.WithCancel(context.Background())
	t.Cleanup(cancel)
	go func() { code, err := g.Run(ctx, signals); done <- guardRun{code, err} }()
	return signals, done
}

func wait(t *testing.T, done chan guardRun, within time.Duration) guardRun {
	t.Helper()
	select {
	case r := <-done:
		return r
	case <-time.After(within):
		t.Fatalf("guard still running after %s", within)
		return guardRun{}
	}
}

func alive(pid int) bool {
	// A zombie still answers kill(0); read its state instead.
	b, err := os.ReadFile("/proc/" + strconv.Itoa(pid) + "/stat")
	return err == nil && !strings.Contains(string(b), ") Z ")
}

func TestTheGuardNeverStartsTheControllerWhileTheGateIsClosed(t *testing.T) {
	dir := t.TempDir()
	marker := filepath.Join(dir, "started")
	signals, done := startGuard(t, filepath.Join(dir, "gate"), "touch "+marker)
	time.Sleep(300 * time.Millisecond)
	signals <- syscall.SIGTERM
	if r := wait(t, done, 2*time.Second); r.code != 0 || r.err != nil {
		t.Fatalf("got %+v", r)
	}
	if exists(marker) {
		t.Fatal("the controller started without the gate")
	}
}

func TestAStaleGateKillsTheControllerAndEverythingItStarted(t *testing.T) {
	dir := t.TempDir()
	gate, pidfile := filepath.Join(dir, "gate"), filepath.Join(dir, "pid")
	if err := os.WriteFile(gate, []byte("epoch=0\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	// The agent keeps renewing for a while, then stops (it hung, or the node lost the API).
	stop := make(chan struct{})
	go func() {
		for {
			select {
			case <-stop:
				return
			case <-time.After(100 * time.Millisecond):
				now := time.Now()
				_ = os.Chtimes(gate, now, now)
			}
		}
	}()
	_, done := startGuard(t, gate, "sleep 300 & echo $! > "+pidfile+"; wait")
	eventually(t, "controller started", func() bool { return exists(pidfile) })
	time.Sleep(time.Second) // renewals keep it running well past Stale
	select {
	case r := <-done:
		t.Fatalf("killed while the gate was fresh: %+v", r)
	default:
	}
	b, _ := os.ReadFile(pidfile)
	child, _ := strconv.Atoi(strings.TrimSpace(string(b)))

	close(stop)
	stoppedAt := time.Now()
	r := wait(t, done, 3*time.Second)
	if !errors.Is(r.err, ErrGateClosed) || r.code != 137 {
		t.Fatalf("got %+v", r)
	}
	if took := time.Since(stoppedAt); took > 600*time.Millisecond+100*time.Millisecond+150*time.Millisecond {
		t.Fatalf("killed %s after the last refresh", took)
	}
	eventually(t, "grandchild killed", func() bool { return !alive(child) })
}

func TestARemovedGateKillsTheController(t *testing.T) {
	dir := t.TempDir()
	gate := filepath.Join(dir, "gate")
	if err := os.WriteFile(gate, nil, 0o644); err != nil {
		t.Fatal(err)
	}
	_, done := startGuard(t, gate, "sleep 300")
	time.Sleep(200 * time.Millisecond)
	if err := os.Remove(gate); err != nil {
		t.Fatal(err)
	}
	if r := wait(t, done, 2*time.Second); !errors.Is(r.err, ErrGateClosed) {
		t.Fatalf("got %+v", r)
	}
}

func TestTheGuardPassesSignalsOnAndReturnsTheControllersExitCode(t *testing.T) {
	dir := t.TempDir()
	gate, ready := filepath.Join(dir, "gate"), filepath.Join(dir, "ready")
	if err := os.WriteFile(gate, nil, 0o644); err != nil {
		t.Fatal(err)
	}
	go func() {
		for i := 0; i < 100; i++ {
			now := time.Now()
			_ = os.Chtimes(gate, now, now)
			time.Sleep(50 * time.Millisecond)
		}
	}()
	signals, done := startGuard(t, gate, `trap "exit 7" TERM; touch `+ready+`; while :; do sleep 0.05; done`)
	eventually(t, "trap installed", func() bool { return exists(ready) })
	signals <- syscall.SIGTERM
	if r := wait(t, done, 3*time.Second); r.code != 7 || r.err != nil {
		t.Fatalf("got %+v", r)
	}

	_, done = startGuard(t, gate, "exit 3")
	if r := wait(t, done, 3*time.Second); r.code != 3 || r.err != nil {
		t.Fatalf("got %+v", r)
	}
}
