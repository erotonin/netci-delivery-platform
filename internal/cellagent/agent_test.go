package cellagent

import (
	"context"
	"io"
	"log/slog"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"testing"
	"time"

	coordinationv1 "k8s.io/api/coordination/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/utils/clock"

	"github.com/prometheus/client_golang/prometheus"

	"github.com/erotonin/netci-delivery-platform/internal/lease"
	"github.com/erotonin/netci-delivery-platform/internal/lease/leasetest"
)

// fakeProc is a directory laid out like /proc whose "processes" die when sent SIGKILL.
type fakeProc struct {
	root   string
	mu     sync.Mutex
	killed []int
}

func newFakeProc(t *testing.T) *fakeProc { return &fakeProc{root: t.TempDir()} }

func (f *fakeProc) start(t *testing.T, pid int, cmdline string) {
	t.Helper()
	dir := filepath.Join(f.root, strconv.Itoa(pid))
	if err := os.MkdirAll(dir, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "cmdline"), []byte(strings.ReplaceAll(cmdline, " ", "\x00")), 0o644); err != nil {
		t.Fatal(err)
	}
}

func (f *fakeProc) processes(self int) *Processes {
	return &Processes{ProcRoot: f.root, Match: "jenkins.war", Self: self, Signal: func(pid int, sig syscall.Signal) error {
		f.mu.Lock()
		defer f.mu.Unlock()
		f.killed = append(f.killed, pid)
		if sig == syscall.SIGKILL {
			return os.RemoveAll(filepath.Join(f.root, strconv.Itoa(pid)))
		}
		return nil
	}}
}

func (f *fakeProc) kills() []int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return append([]int(nil), f.killed...)
}

func TestOnlyTheControllerIsKilledAndNeverTheAgentItself(t *testing.T) {
	f := newFakeProc(t)
	f.start(t, 10, "java -jar /usr/share/jenkins/jenkins.war --httpPort=8080")
	f.start(t, 11, "sleep 60")
	f.start(t, 12, "cell-agent --match jenkins.war") // the agent's own command line names the match
	p := f.processes(12)
	if n := p.KillJenkins(); n != 1 {
		t.Fatalf("killed %d processes", n)
	}
	if got := f.kills(); len(got) != 1 || got[0] != 10 {
		t.Fatalf("signalled %v", got)
	}
	if p.JenkinsRunning() {
		t.Fatal("controller still reported running")
	}
}

func agent(t *testing.T, api *leasetest.APIServer, f *fakeProc, identity string) (*Agent, string) {
	gate := filepath.Join(t.TempDir(), "gate")
	h := &lease.Holder{
		Leases: api, Namespace: "cell-a", Name: "netci-cell", Identity: identity,
		Duration: time.Second, RenewInterval: 100 * time.Millisecond, RenewDeadline: 500 * time.Millisecond,
		Clock: clock.RealClock{}, Observer: lease.NewObserver(nil),
		Log: slog.New(slog.NewTextHandler(io.Discard, nil)),
	}
	a := &Agent{Holder: h, GateFile: gate, Procs: f.processes(-1), KeepDeadEvery: 50 * time.Millisecond,
		Log: slog.New(slog.NewTextHandler(io.Discard, nil)), Metrics: NewMetrics(prometheus.NewRegistry())}
	a.Wire()
	return a, gate
}

func eventually(t *testing.T, what string, cond func() bool) {
	t.Helper()
	deadline := time.Now().Add(5 * time.Second)
	for !cond() {
		if time.Now().After(deadline) {
			t.Fatalf("timed out: %s", what)
		}
		time.Sleep(20 * time.Millisecond)
	}
}

func exists(path string) bool { _, err := os.Stat(path); return err == nil }

func TestTheGateOpensWithTheLeaseAndTheControllerDiesWhenItIsLost(t *testing.T) {
	api, f := leasetest.New(), newFakeProc(t)
	a, gate := agent(t, api, f, "jenkins-0/a")
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go a.Run(ctx)

	eventually(t, "gate open", func() bool { return exists(gate) })
	if b, _ := os.ReadFile(gate); !strings.Contains(string(b), "epoch=0") {
		t.Fatalf("gate content %q", b)
	}
	first, _ := os.Stat(gate)
	eventually(t, "gate refreshed by renewals", func() bool {
		fi, err := os.Stat(gate)
		return err == nil && fi.ModTime().After(first.ModTime())
	})
	if fi, _ := os.Stat(gate); time.Since(fi.ModTime()) > a.Holder.RenewInterval*3 {
		t.Fatalf("gate mtime %s is not the last renewal", fi.ModTime())
	}
	f.start(t, 42, "java -jar jenkins.war")

	api.SetUnreachable(true) // the node is cut off from the API server
	lostBy := time.Now().Add(a.Holder.RenewDeadline + 300*time.Millisecond)
	eventually(t, "gate closed", func() bool { return !exists(gate) })
	eventually(t, "controller killed", func() bool { return len(f.kills()) > 0 })
	if time.Now().After(lostBy.Add(200 * time.Millisecond)) {
		t.Fatal("the controller outlived the renew deadline")
	}
}

func TestAControllerStartedWithoutTheLeaseIsKilled(t *testing.T) {
	api, f := leasetest.New(), newFakeProc(t)
	// Another pod holds the lease and keeps renewing it.
	other := "jenkins-0/other"
	seconds, zero := int32(1), int32(0)
	now := metav1.NewMicroTime(time.Now())
	if _, err := api.Leases("cell-a").Create(context.Background(), &coordinationv1.Lease{
		ObjectMeta: metav1.ObjectMeta{Name: "netci-cell"},
		Spec:       coordinationv1.LeaseSpec{HolderIdentity: &other, LeaseDurationSeconds: &seconds, RenewTime: &now, LeaseTransitions: &zero},
	}, metav1.CreateOptions{}); err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go func() {
		for ctx.Err() == nil {
			l := api.Get("cell-a", "netci-cell")
			_, _ = api.Leases("cell-a").Update(ctx, l, metav1.UpdateOptions{})
			time.Sleep(100 * time.Millisecond)
		}
	}()
	a, gate := agent(t, api, f, "jenkins-0/a")
	go a.Run(ctx)
	f.start(t, 77, "java -jar jenkins.war") // e.g. the container restarted and skipped the gate
	eventually(t, "unleased controller killed", func() bool { return len(f.kills()) > 0 })
	time.Sleep(1500 * time.Millisecond)
	if exists(gate) {
		t.Fatal("gate opened while another pod renews the lease")
	}
}

func TestShutdownKeepsTheLeaseUntilTheControllerHasExitedThenReleasesIt(t *testing.T) {
	api, f := leasetest.New(), newFakeProc(t)
	a, gate := agent(t, api, f, "jenkins-0/a")
	ctx, cancel := context.WithCancel(context.Background())
	go a.Run(ctx)
	eventually(t, "gate open", func() bool { return exists(gate) })
	f.start(t, 42, "java -jar jenkins.war")

	cancel() // SIGTERM reached the pod
	done := make(chan struct{})
	go func() { a.Shutdown(context.Background(), 10*time.Second); close(done) }()
	time.Sleep(700 * time.Millisecond) // longer than the renew deadline: renewal must continue
	if l := api.Get("cell-a", "netci-cell"); l.Spec.HolderIdentity == nil || *l.Spec.HolderIdentity != "jenkins-0/a" {
		t.Fatal("the lease went before the controller exited")
	}
	os.RemoveAll(filepath.Join(f.root, "42")) // Jenkins finished its orderly shutdown
	select {
	case <-done:
	case <-time.After(5 * time.Second):
		t.Fatal("shutdown did not finish")
	}
	if l := api.Get("cell-a", "netci-cell"); l.Spec.HolderIdentity != nil {
		t.Fatalf("lease not released: %q", *l.Spec.HolderIdentity)
	}
	time.Sleep(500 * time.Millisecond)
	if l := api.Get("cell-a", "netci-cell"); l.Spec.HolderIdentity != nil {
		t.Fatal("the lease was taken back after the release")
	}
	if exists(gate) {
		t.Fatal("gate left open after shutdown")
	}
}
