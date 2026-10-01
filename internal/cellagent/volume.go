package cellagent

import (
	"context"
	"fmt"
	"log/slog"
	"os"
	"path/filepath"
	"sync/atomic"
	"time"

	"github.com/erotonin/netci-delivery-platform/internal/lease"
)

// VolumeFailedAnnotation is how the probe's report reaches the supervisor (lease.Holder.Report).
const VolumeFailedAnnotation = lease.VolumeFailedAnnotation

// VolumeProbe writes to JENKINS_HOME at an interval and reports when it no longer can.
//
// A volume whose storage engine failed under a running pod -- in the lab, Longhorn re-attaching it
// after its engine died -- leaves the pod with a mount that fails every write, and nothing in
// Kubernetes restarts the pod for that: the controller runs on, broken. Longhorn can delete such
// pods itself, but it decides by comparing start times, and a replacement that a fast takeover
// started first was deleted too (twice in a chaos series of 6). This probe sees the failure from
// inside the pod, whatever the storage, and the supervisor restarts that pod only.
type VolumeProbe struct {
	Dir      string // in JENKINS_HOME; the probe writes a file in it
	Every    time.Duration
	Timeout  time.Duration // a write that has not finished by then is a failure (hung I/O)
	Failures int           // consecutive failures before the volume is reported failed
	// Report is told when the volume fails (detail set) and when it works again (detail "").
	Report func(detail string)
	Log    *slog.Logger
	// write is replaced by tests.
	write func(path string) error

	inFlight atomic.Bool
}

// Run probes until ctx is done.
func (p *VolumeProbe) Run(ctx context.Context) {
	if p.write == nil {
		p.write = writeSynced
	}
	t := time.NewTicker(p.Every)
	defer t.Stop()
	failed, reported := 0, false
	for {
		err := p.once(ctx)
		if ctx.Err() != nil {
			return // stopping is not the volume working again
		}
		switch {
		case err == nil:
			if reported {
				p.Log.Info("JENKINS_HOME takes writes again", "dir", p.Dir)
				p.Report("")
			}
			failed, reported = 0, false
		default:
			failed++
			p.Log.Warn("JENKINS_HOME write failed", "dir", p.Dir, "consecutive", failed, "error", err)
			if failed >= p.Failures && !reported {
				p.Report(fmt.Sprintf("%d writes failed in a row, the last: %v", failed, err))
				reported = true
			}
		}
		select {
		case <-ctx.Done():
			return
		case <-t.C:
		}
	}
}

// once writes the probe file. A write still hanging from before is a failure, and no second one
// is started beside it.
func (p *VolumeProbe) once(ctx context.Context) error {
	if !p.inFlight.CompareAndSwap(false, true) {
		return fmt.Errorf("the previous write has not returned")
	}
	done := make(chan error, 1)
	go func() {
		defer p.inFlight.Store(false)
		done <- p.write(filepath.Join(p.Dir, "volume-probe"))
	}()
	timer := time.NewTimer(p.Timeout)
	defer timer.Stop()
	select {
	case err := <-done:
		return err
	case <-timer.C:
		return fmt.Errorf("no answer from the volume in %s", p.Timeout)
	case <-ctx.Done():
		return nil
	}
}

// writeSynced writes a small file and makes the storage confirm it: a new file, fsynced, renamed
// over the old one, and the directory fsynced.
func writeSynced(path string) error {
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return err
	}
	tmp := path + ".tmp"
	f, err := os.OpenFile(tmp, os.O_CREATE|os.O_WRONLY|os.O_TRUNC, 0o600)
	if err != nil {
		return err
	}
	if _, err := f.WriteString(time.Now().UTC().Format(time.RFC3339Nano) + "\n"); err != nil {
		f.Close()
		return err
	}
	if err := f.Sync(); err != nil {
		f.Close()
		return err
	}
	if err := f.Close(); err != nil {
		return err
	}
	if err := os.Rename(tmp, path); err != nil {
		return err
	}
	d, err := os.Open(filepath.Dir(path))
	if err != nil {
		return err
	}
	defer d.Close()
	return d.Sync()
}
