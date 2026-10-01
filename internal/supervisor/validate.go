package supervisor

import (
	"context"
	"fmt"
	"strings"
	"time"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/kubernetes"

	"github.com/erotonin/netci-delivery-platform/internal/fence"
)

// ParseMachines reads "node=machine,node=machine". Each node and each machine may appear once:
// two nodes on one machine would have one fencing take out both.
func ParseMachines(s string) (map[string]string, error) {
	out, machines := map[string]string{}, map[string]bool{}
	for _, pair := range strings.Split(s, ",") {
		pair = strings.TrimSpace(pair)
		if pair == "" {
			continue
		}
		node, machine, ok := strings.Cut(pair, "=")
		node, machine = strings.TrimSpace(node), strings.TrimSpace(machine)
		if !ok || node == "" || machine == "" {
			return nil, fmt.Errorf("machine mapping %q: want node=machine", pair)
		}
		if _, dup := out[node]; dup {
			return nil, fmt.Errorf("node %s is mapped twice", node)
		}
		if machines[machine] {
			return nil, fmt.Errorf("machine %s is mapped to two nodes", machine)
		}
		out[node], machines[machine] = machine, true
	}
	if len(out) == 0 {
		return nil, fmt.Errorf("no node-to-machine mapping: the supervisor could fence nothing")
	}
	return out, nil
}

// Retryable marks a startup check that could not be completed (the API server or the power
// controller did not answer): the supervisor waits and checks again rather than exit. A
// supervisor restarted in the middle of an outage must come up once it can see, not crash-loop
// through the outage it is there to handle.
type Retryable struct{ error }

func (r Retryable) Unwrap() error { return r.error }

// ValidateMachines refuses to start on a mapping that contradicts what can be seen: every
// mapped node must exist, and a node whose kubelet is alive must be on a machine its power
// controller reports running. A mapping that points at the wrong machine would power off a
// healthy one.
//
// "Alive" is not the node's Ready condition: that lags by 40 s, so a node that has just lost
// power still reads Ready while its machine is off -- the very failure a restarted supervisor
// may come up into. A node in that state is re-checked after heartbeatWait: if its kubelet's
// heartbeat Lease was renewed meanwhile and its machine still is not running, the mapping is
// wrong; if not, it is a dead node and not a reason to refuse.
func ValidateMachines(ctx context.Context, client kubernetes.Interface, f fence.Fencer, machines map[string]string, timeout, heartbeatWait time.Duration) error {
	nodes, err := client.CoreV1().Nodes().List(ctx, metav1.ListOptions{})
	if err != nil {
		return Retryable{fmt.Errorf("list nodes: %w", err)}
	}
	known := map[string]bool{}
	suspect := map[string]string{} // node -> its heartbeat's renewTime when first seen
	var unreachable []string
	for i := range nodes.Items {
		n := &nodes.Items[i]
		known[n.Name] = true
		machine, ok := machines[n.Name]
		if !ok {
			continue // a node without a mapping is never fenced; Decide alerts for it
		}
		state, err := machineState(ctx, f, machine, timeout)
		switch {
		case err != nil:
			unreachable = append(unreachable, fmt.Sprintf("%s: power controller %s cannot report machine %s: %v", n.Name, f.Name(), machine, err))
		case ready(n) && state != fence.Running:
			beat, err := heartbeat(ctx, client, n.Name)
			if err != nil {
				return Retryable{err}
			}
			suspect[n.Name] = beat
		}
	}
	var problems []string
	for node := range machines {
		if !known[node] {
			problems = append(problems, fmt.Sprintf("mapped node %s does not exist", node))
		}
	}
	if len(problems) > 0 {
		return fmt.Errorf("refusing to start: %s", strings.Join(problems, "; "))
	}
	if len(unreachable) > 0 {
		return Retryable{fmt.Errorf("%s", strings.Join(unreachable, "; "))}
	}
	if len(suspect) == 0 {
		return nil
	}
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-time.After(heartbeatWait):
	}
	for node, before := range suspect {
		after, err := heartbeat(ctx, client, node)
		if err != nil {
			return Retryable{err}
		}
		if after == before {
			continue // no heartbeat: a dead node, which is what the power controller says
		}
		state, err := machineState(ctx, f, machines[node], timeout)
		if err != nil {
			return Retryable{err}
		}
		if state != fence.Running {
			problems = append(problems, fmt.Sprintf("%s's kubelet is alive but its machine %s reports %s: the mapping is wrong", node, machines[node], state))
		}
	}
	if len(problems) > 0 {
		return fmt.Errorf("refusing to start: %s", strings.Join(problems, "; "))
	}
	return nil
}

func machineState(ctx context.Context, f fence.Fencer, machine string, timeout time.Duration) (fence.State, error) {
	qctx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	return f.State(qctx, machine)
}

// heartbeat is the node's kubelet Lease renewTime as written: compared only with itself, never
// with this process's clock.
func heartbeat(ctx context.Context, client kubernetes.Interface, node string) (string, error) {
	l, err := client.CoordinationV1().Leases(nodeLeaseNS).Get(ctx, node, metav1.GetOptions{})
	if err != nil {
		return "", fmt.Errorf("heartbeat of %s: %w", node, err)
	}
	if l.Spec.RenewTime == nil {
		return "", nil
	}
	return l.Spec.RenewTime.UTC().Format(time.RFC3339Nano), nil
}
