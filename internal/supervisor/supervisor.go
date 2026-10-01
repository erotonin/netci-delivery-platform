package supervisor

import (
	"context"
	"fmt"
	"log/slog"
	"time"

	"k8s.io/utils/clock"
)

// Supervisor observes, decides and acts once per Interval. Run it only in the replica that
// holds the supervisor's own leader lease.
type Supervisor struct {
	Collector *Collector
	Executor  *Executor
	Config    Config
	Interval  time.Duration
	// ActionTimeout bounds one action, powering a machine off included.
	ActionTimeout time.Duration
	// AlertEvery: the same alert is reported at most this often (it is decided every tick).
	AlertEvery time.Duration
	Clock      clock.PassiveClock
	Log        *slog.Logger
	Metrics    *Metrics

	alerted   map[string]time.Time
	takeovers map[string]takeover
	lastErr   string
}

// takeover follows one cell from the supervisor seeing its holder stop to a new holder.
type takeover struct {
	lastRenewal time.Time // the last change the supervisor saw before the Lease went quiet
	epoch       int32
	holder      string
}

// Run ticks until ctx is done.
func (s *Supervisor) Run(ctx context.Context) {
	t := time.NewTicker(s.Interval)
	defer t.Stop()
	for {
		s.Tick(ctx)
		select {
		case <-ctx.Done():
			return
		case <-t.C:
		}
	}
}

// Tick is one observation, decision and round of actions.
func (s *Supervisor) Tick(ctx context.Context) {
	if s.alerted == nil {
		s.alerted, s.takeovers = map[string]time.Time{}, map[string]takeover{}
	}
	snap, err := s.Collector.Collect(ctx)
	if err != nil {
		s.Metrics.observeErrors.Inc()
		if msg := err.Error(); msg != s.lastErr {
			s.Log.Warn("cannot observe the cluster; deciding nothing until it can", "error", err)
			s.lastErr = msg
		}
		return
	}
	if s.lastErr != "" {
		s.Log.Info("observing the cluster again")
		s.lastErr = ""
	}
	if snap.Reset {
		s.Metrics.resets.Inc()
		s.Log.Info("observation (re)started: nothing is called expired until watched for a full lease duration")
	}
	s.track(snap.Input)

	for _, a := range Decide(snap.Input, s.Config) {
		if a.Kind == Alert && !s.shouldAlert(a, snap.Input.Now) {
			continue
		}
		if a.Kind == Alert {
			s.Log.Warn("needs a person", "cell", a.Namespace+"/"+a.Cell, "node", a.Node, "reason", a.Reason)
		} else {
			s.Log.Warn("acting", "action", a.Kind, "cell", a.Namespace+"/"+a.Cell, "node", a.Node,
				"machine", a.Machine, "power_off", a.PowerOff, "reason", a.Reason)
		}
		actx, cancel := context.WithTimeout(ctx, s.ActionTimeout)
		err := s.Executor.Execute(actx, a, snap.Leases[a.Namespace+"/"+a.Cell])
		cancel()
		if err != nil {
			s.Log.Error("action failed; it will be decided again from a fresh observation",
				"action", a.Kind, "cell", a.Namespace+"/"+a.Cell, "node", a.Node, "error", err)
		}
	}
}

// track measures takeovers: from the last renewal seen before a cell's Lease went quiet to a
// different pod holding it.
func (s *Supervisor) track(in Input) {
	var lost int
	for _, c := range in.Cells {
		key := c.Namespace + "/" + c.Name
		if c.holdsLease() && c.LeaseExpired {
			lost++
			if _, ok := s.takeovers[key]; !ok {
				s.takeovers[key] = takeover{lastRenewal: in.Now.Add(-c.LeaseUnchanged), epoch: c.LeaseEpoch, holder: c.LeaseHolder}
			}
			continue
		}
		t, ok := s.takeovers[key]
		if !ok || !c.holdsLease() {
			continue
		}
		delete(s.takeovers, key)
		took := in.Now.Sub(t.lastRenewal)
		if c.LeaseHolder == t.holder {
			s.Log.Info("the same pod renewed its lease again; no takeover", "cell", key, "quiet_for", took)
			continue
		}
		s.Metrics.takeoverSeconds.Observe(took.Seconds())
		s.Log.Warn("takeover complete", "cell", key, "seconds", fmt.Sprintf("%.1f", took.Seconds()),
			"from", t.holder, "to", c.LeaseHolder, "epoch", c.LeaseEpoch)
		s.Executor.Events.Event(ObjectRef{Kind: "StatefulSet", Namespace: c.Namespace, Name: c.Name}, false, "TakeoverComplete",
			fmt.Sprintf("%s holds the lease (epoch %d) %.1fs after the last renewal by %s", c.LeaseHolder, c.LeaseEpoch, took.Seconds(), t.holder))
	}
	s.Metrics.cells.Set(float64(len(in.Cells)))
	s.Metrics.lost.Set(float64(lost))
}

func (s *Supervisor) shouldAlert(a Action, now time.Time) bool {
	key := a.Namespace + "/" + a.Cell + "|" + a.Node
	if last, ok := s.alerted[key]; ok && now.Sub(last) < s.AlertEvery {
		return false
	}
	s.alerted[key] = now
	return true
}
