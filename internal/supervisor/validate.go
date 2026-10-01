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

// ValidateMachines refuses to start on a mapping that contradicts what can be seen: every
// mapped node must exist, and a node that is Ready must be on a machine its power controller
// reports running. A mapping that points at the wrong machine would power off a healthy one;
// a power controller that cannot be asked cannot fence.
func ValidateMachines(ctx context.Context, client kubernetes.Interface, f fence.Fencer, machines map[string]string, timeout time.Duration) error {
	nodes, err := client.CoreV1().Nodes().List(ctx, metav1.ListOptions{})
	if err != nil {
		return err
	}
	known := map[string]bool{}
	var problems []string
	for i := range nodes.Items {
		n := &nodes.Items[i]
		known[n.Name] = true
		machine, ok := machines[n.Name]
		if !ok {
			continue // a node without a mapping is never fenced; Decide alerts for it
		}
		qctx, cancel := context.WithTimeout(ctx, timeout)
		state, err := f.State(qctx, machine)
		cancel()
		switch {
		case err != nil:
			problems = append(problems, fmt.Sprintf("%s: power controller %s cannot report machine %s: %v", n.Name, f.Name(), machine, err))
		case ready(n) && state != fence.Running:
			problems = append(problems, fmt.Sprintf("%s is Ready but its machine %s reports %s: the mapping is wrong", n.Name, machine, state))
		}
	}
	for node := range machines {
		if !known[node] {
			problems = append(problems, fmt.Sprintf("mapped node %s does not exist", node))
		}
	}
	if len(problems) > 0 {
		return fmt.Errorf("refusing to start: %s", strings.Join(problems, "; "))
	}
	return nil
}
