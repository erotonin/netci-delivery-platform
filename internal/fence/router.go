package fence

import (
	"context"
	"fmt"
	"sort"
	"strings"
)

// Target is how one machine is reached: the Fencer that controls its power, and the name that
// Fencer knows it by (a libvirt domain, a Redfish system path, ...).
type Target struct {
	Fencer Fencer
	ID     string
}

// Router is a Fencer over machines whose power controllers differ: every server has its own
// BMC, and a cluster may mix virtual machines with bare metal. Machines are the names the
// supervisor uses; each is routed to its own Target.
type Router struct {
	Targets map[string]Target
}

// NewRouter refuses two machines that resolve to the same power target: fencing one would cut
// the other's power too, and the decision that allowed the first would not have covered it.
func NewRouter(targets map[string]Target) (*Router, error) {
	seen := map[string]string{}
	for machine, t := range targets {
		if t.Fencer == nil || t.ID == "" {
			return nil, fmt.Errorf("machine %s has no power target", machine)
		}
		key := t.Fencer.Name() + "|" + t.ID
		if other, dup := seen[key]; dup {
			return nil, fmt.Errorf("machines %s and %s resolve to the same power target %s", other, machine, key)
		}
		seen[key] = machine
	}
	return &Router{Targets: targets}, nil
}

func (r *Router) Name() string {
	names := map[string]bool{}
	for _, t := range r.Targets {
		names[t.Fencer.Name()] = true
	}
	list := make([]string, 0, len(names))
	for n := range names {
		list = append(list, n)
	}
	sort.Strings(list)
	return "router[" + strings.Join(list, ",") + "]"
}

func (r *Router) target(machine string) (Target, error) {
	t, ok := r.Targets[machine]
	if !ok {
		return Target{}, fmt.Errorf("no power controller is configured for machine %s", machine)
	}
	return t, nil
}

func (r *Router) State(ctx context.Context, machine string) (State, error) {
	t, err := r.target(machine)
	if err != nil {
		return Unknown, err
	}
	return t.Fencer.State(ctx, t.ID)
}

func (r *Router) PowerOff(ctx context.Context, machine string) error {
	t, err := r.target(machine)
	if err != nil {
		return err
	}
	return t.Fencer.PowerOff(ctx, t.ID)
}

func (r *Router) PowerOn(ctx context.Context, machine string) error {
	t, err := r.target(machine)
	if err != nil {
		return err
	}
	return t.Fencer.PowerOn(ctx, t.ID)
}
