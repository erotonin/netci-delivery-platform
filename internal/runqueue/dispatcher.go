package runqueue

import (
	"context"
	"errors"
	"log/slog"
	"sync"
	"time"
)

// Cell is one Jenkins controller's share of the queue.
type Cell struct {
	Name       string
	Controller Controller
	// Budget: at most this many of the cell's runs sit in its controller's queue at once. The
	// queue that matters is the durable one; Jenkins' own stays short.
	Budget int
}

// Dispatcher hands accepted runs to their cell's controller and follows them until their build
// ends. It is a reconciler: every pass compares what the queue says with what the controller
// says, so a crashed controller, a lost reply or a second dispatcher only cost a repeated,
// idempotent call.
type Dispatcher struct {
	Store *Store
	Cells []Cell
	Batch int
	// Lease: how long a claimed run is left alone by other dispatchers.
	Lease time.Duration
	// Poll: how soon a run in a controller is looked at again.
	Poll    time.Duration
	Log     *slog.Logger
	Metrics *Metrics
}

// Run ticks every interval until ctx is done.
func (d *Dispatcher) Run(ctx context.Context, interval time.Duration) {
	t := time.NewTicker(interval)
	defer t.Stop()
	for {
		d.Tick(ctx)
		select {
		case <-ctx.Done():
			return
		case <-t.C:
		}
	}
}

// Tick is one pass over every cell, cells in parallel: a slow controller delays only its own.
func (d *Dispatcher) Tick(ctx context.Context) {
	var wg sync.WaitGroup
	for _, c := range d.Cells {
		wg.Add(1)
		go func(c Cell) {
			defer wg.Done()
			d.cell(ctx, c)
		}(c)
	}
	wg.Wait()
}

func (d *Dispatcher) cell(ctx context.Context, c Cell) {
	open, err := d.Store.Claim(ctx, c.Name, []State{Dispatched, Started}, d.Batch, d.Lease)
	if err != nil {
		d.Log.Error("claim open runs", "cell", c.Name, "error", err)
		return
	}
	for _, r := range open {
		d.track(ctx, c, r)
	}
	out, err := d.Store.Outstanding(ctx, c.Name)
	if err != nil {
		d.Log.Error("count outstanding runs", "cell", c.Name, "error", err)
		return
	}
	room := c.Budget - out
	if room > d.Batch {
		room = d.Batch
	}
	accepted, err := d.Store.Claim(ctx, c.Name, []State{Accepted}, room, d.Lease)
	if err != nil {
		d.Log.Error("claim accepted runs", "cell", c.Name, "error", err)
		return
	}
	for _, r := range accepted {
		d.dispatch(ctx, c, r, "")
	}
}

// dispatch asks the controller to run r (or tells where it already is).
func (d *Dispatcher) dispatch(ctx context.Context, c Cell, r *Run, why string) {
	st, err := c.Controller.Dispatch(ctx, r, r.Client)
	if err != nil {
		d.failed(ctx, c, r, err)
		return
	}
	detail := map[string]any{"session": st.Session, "created": st.Created}
	if why != "" {
		detail["why"] = why
	}
	d.apply(ctx, c, r, st, detail)
}

// track compares a run in a controller with what the controller says now.
func (d *Dispatcher) track(ctx context.Context, c Cell, r *Run) {
	st, found, err := c.Controller.Lookup(ctx, r)
	if err != nil {
		d.failed(ctx, c, r, err)
		return
	}
	if found {
		var detail map[string]any
		if st.Session != r.Session {
			detail = map[string]any{"session": st.Session, "why": "the controller restarted and still has the run"}
		}
		d.apply(ctx, c, r, st, detail)
		return
	}
	switch {
	case r.State == Dispatched && st.Session != r.Session:
		// The controller that had it in its queue is gone, and its queue with it (JENKINS-30909).
		// The cell's lease guarantees that controller is not running anywhere; this one has
		// never seen the run, so dispatching it again runs it once.
		d.Metrics.redispatched.WithLabelValues(c.Name).Inc()
		d.Log.Warn("run lost with its controller's queue: dispatching it again", "cell", c.Name, "run", r.ID, "job", r.Job)
		d.dispatch(ctx, c, r, "lost from the queue of a controller that restarted")
	case r.State == Dispatched:
		// The same controller had it queued and now has neither the item nor a build: it was
		// cancelled in Jenkins. Dispatching it again would overrule whoever cancelled it.
		d.transition(ctx, c, r, Update{To: Cancelled, Detail: map[string]any{"why": "removed from the controller's queue without starting"}})
	default: // Started, and the build record is gone (deleted, or discarded by the job's retention)
		d.transition(ctx, c, r, Update{To: Finished, Result: "UNKNOWN", Detail: map[string]any{"why": "the build record no longer exists"}})
	}
}

// apply records what the controller said about r.
func (d *Dispatcher) apply(ctx context.Context, c Cell, r *Run, st *Status, detail map[string]any) {
	switch st.State {
	case "queued", "starting":
		if r.State == Started {
			return // a started run cannot go back; a stale answer
		}
		d.transition(ctx, c, r, Update{To: Dispatched, Session: st.Session, QueueID: st.QueueID, NextAttempt: d.Poll, Detail: detail})
	case "started":
		if st.Build == nil {
			d.failed(ctx, c, r, errors.New("controller said started without a build"))
			return
		}
		if r.State != Started {
			if !d.transition(ctx, c, r, Update{To: Started, Session: st.Session, BuildNumber: st.Build.Number, BuildURL: st.Build.URL,
				NextAttempt: d.Poll, Detail: map[string]any{"build": st.Build.Number}}) {
				return
			}
			d.Metrics.acceptToStart.WithLabelValues(c.Name).Observe(time.Since(r.AcceptedAt).Seconds())
			r.State = Started
		}
		if !st.Build.Building {
			d.transition(ctx, c, r, Update{To: Finished, Result: st.Build.Result, Detail: map[string]any{"result": st.Build.Result}})
			return
		}
		if detail != nil || st.Session != r.Session {
			d.transition(ctx, c, r, Update{To: Started, Session: st.Session, NextAttempt: d.Poll, Detail: detail})
		} else {
			d.transition(ctx, c, r, Update{To: Started, NextAttempt: d.Poll})
		}
	default:
		d.failed(ctx, c, r, errors.New("controller answered an unknown state "+st.State))
	}
}

// transition applies u; another dispatcher having moved the run on first is not an error.
func (d *Dispatcher) transition(ctx context.Context, c Cell, r *Run, u Update) bool {
	err := d.Store.Transition(ctx, r.ID, r.State, u)
	switch {
	case err == nil:
		if u.To != r.State {
			d.Metrics.transitions.WithLabelValues(c.Name, string(u.To)).Inc()
			d.Log.Info("run", "cell", c.Name, "run", r.ID, "job", r.Job, "from", r.State, "to", u.To, "build", u.BuildNumber, "result", u.Result)
		}
		return true
	case errors.Is(err, ErrConflict):
		return false
	default:
		d.Log.Error("record transition", "run", r.ID, "to", u.To, "error", err)
		return false
	}
}

func (d *Dispatcher) failed(ctx context.Context, c Cell, r *Run, err error) {
	var perm *PermanentError
	if errors.As(err, &perm) {
		if r.State == Started {
			d.transition(ctx, c, r, Update{To: Finished, Result: "UNKNOWN", Detail: map[string]any{"why": perm.Error()}})
			return
		}
		d.transition(ctx, c, r, Update{To: Refused, Detail: map[string]any{"status": perm.Status, "message": perm.Message}})
		return
	}
	d.Metrics.errors.WithLabelValues(c.Name).Inc()
	backoff := time.Second << min(r.Attempts, 6)
	if backoff > time.Minute {
		backoff = time.Minute
	}
	d.Log.Warn("controller call failed; retrying", "cell", c.Name, "run", r.ID, "attempt", r.Attempts+1, "in", backoff, "error", err)
	if err := d.Store.Retry(ctx, r.ID, r.State, err, backoff); err != nil {
		d.Log.Error("record retry", "run", r.ID, "error", err)
	}
}
