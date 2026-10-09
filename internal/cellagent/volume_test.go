package cellagent

import (
	"context"
	"io"
	"log/slog"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"syscall"
	"testing"
	"time"
)

type reports struct {
	mu  sync.Mutex
	got []string
}

func (r *reports) add(d string) {
	r.mu.Lock()
	r.got = append(r.got, d)
	r.mu.Unlock()
}

func (r *reports) list() []string {
	r.mu.Lock()
	defer r.mu.Unlock()
	return append([]string(nil), r.got...)
}

func probe(t *testing.T, write func(string) error) (*VolumeProbe, *reports) {
	t.Helper()
	r := &reports{}
	return &VolumeProbe{Dir: t.TempDir(), Every: 5 * time.Millisecond, Timeout: 50 * time.Millisecond, Failures: 3,
		Report: r.add, Log: slog.New(slog.NewTextHandler(io.Discard, nil)), write: write}, r
}

func runFor(p *VolumeProbe, d time.Duration) {
	ctx, cancel := context.WithTimeout(context.Background(), d)
	defer cancel()
	p.Run(ctx)
}

// Writes that succeed are never reported. Not with real fsyncs: on a loaded CI runner one fsync
// outlasted the test's 50 ms timeout, the next ticks found it still running, and the probe
// reported a healthy disk (CI run 37883975435). Real I/O is checked once, below.
func TestAWorkingVolumeIsNeverReported(t *testing.T) {
	var mu sync.Mutex
	writes := 0
	p, r := probe(t, func(string) error {
		mu.Lock()
		writes++
		mu.Unlock()
		return nil
	})
	runFor(p, 100*time.Millisecond)
	if got := r.list(); len(got) != 0 {
		t.Fatalf("reported %v", got)
	}
	mu.Lock()
	defer mu.Unlock()
	if writes < 3 {
		t.Fatalf("%d writes in 100 ms at a 5 ms interval", writes)
	}
}

func TestTheProbeWritesAndSyncsItsFile(t *testing.T) {
	path := filepath.Join(t.TempDir(), ".netci", "volume-probe")
	for i := 0; i < 2; i++ { // the second replaces the first
		if err := writeSynced(path); err != nil {
			t.Fatal(err)
		}
	}
	b, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := time.Parse(time.RFC3339Nano, strings.TrimSpace(string(b))); err != nil {
		t.Fatalf("the probe file holds %q: %v", b, err)
	}
	if _, err := os.Stat(path + ".tmp"); !os.IsNotExist(err) {
		t.Fatalf("the temporary file was left behind: %v", err)
	}
}

// What a dead mount answers: every write fails. Reported once, after the threshold.
func TestAVolumeThatFailsEveryWriteIsReportedOnce(t *testing.T) {
	var mu sync.Mutex
	calls := 0
	p, r := probe(t, func(string) error {
		mu.Lock()
		calls++
		mu.Unlock()
		return &os.PathError{Op: "write", Path: "volume-probe", Err: syscall.EIO}
	})
	runFor(p, 150*time.Millisecond)
	got := r.list()
	if len(got) != 1 || !strings.Contains(got[0], "input/output error") {
		t.Fatalf("reports %q, want one failure naming the error", got)
	}
}

func TestOneFailedWriteIsNotEnough(t *testing.T) {
	var mu sync.Mutex
	n := 0
	p, r := probe(t, func(string) error {
		mu.Lock()
		defer mu.Unlock()
		n++
		if n%3 == 0 { // every third write fails: never three in a row
			return syscall.EIO
		}
		return nil
	})
	runFor(p, 150*time.Millisecond)
	if got := r.list(); len(got) != 0 {
		t.Fatalf("a passing failure was reported: %v", got)
	}
}

// A hung write (an NFS server gone) is a failure too, and no second write piles up beside it.
func TestAHungWriteIsAFailureAndNotRepeated(t *testing.T) {
	release := make(chan struct{})
	var mu sync.Mutex
	started := 0
	p, r := probe(t, func(string) error {
		mu.Lock()
		started++
		mu.Unlock()
		<-release
		return nil
	})
	runFor(p, 300*time.Millisecond)
	close(release)
	if got := r.list(); len(got) != 1 {
		t.Fatalf("reports %v, want the hang reported once", got)
	}
	mu.Lock()
	defer mu.Unlock()
	if started != 1 {
		t.Fatalf("%d writes started while the first hung", started)
	}
}

func TestAVolumeThatRecoversIsReportedWorking(t *testing.T) {
	var mu sync.Mutex
	broken := true
	p, r := probe(t, func(string) error {
		mu.Lock()
		defer mu.Unlock()
		if broken {
			return syscall.EIO
		}
		return nil
	})
	go func() {
		time.Sleep(60 * time.Millisecond)
		mu.Lock()
		broken = false
		mu.Unlock()
	}()
	runFor(p, 150*time.Millisecond)
	got := r.list()
	if len(got) != 2 || got[0] == "" || got[1] != "" {
		t.Fatalf("reports %q, want a failure then a recovery", got)
	}
}
