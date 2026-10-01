package supervisor

import (
	"context"
	"errors"
	"strings"
	"testing"
	"time"

	coordinationv1 "k8s.io/api/coordination/v1"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/client-go/kubernetes/fake"
	k8stesting "k8s.io/client-go/testing"

	"github.com/erotonin/netci-delivery-platform/internal/fence"
)

func TestParseMachines(t *testing.T) {
	m, err := ParseMachines(" a=vm-a , b=vm-b,")
	if err != nil || m["a"] != "vm-a" || m["b"] != "vm-b" || len(m) != 2 {
		t.Fatalf("%v %v", m, err)
	}
	for _, bad := range []string{"", "a", "a=", "=vm", "a=x,a=y", "a=x,b=x"} {
		if _, err := ParseMachines(bad); err == nil {
			t.Fatalf("accepted %q", bad)
		}
	}
}

type statePower map[string]fence.State

func (p statePower) Name() string { return "test" }
func (p statePower) State(_ context.Context, m string) (fence.State, error) {
	s, ok := p[m]
	if !ok {
		return fence.Unknown, context.DeadlineExceeded
	}
	return s, nil
}
func (statePower) PowerOff(context.Context, string) error { return nil }
func (statePower) PowerOn(context.Context, string) error  { return nil }

func readyNode(name string) *corev1.Node {
	return &corev1.Node{ObjectMeta: metav1.ObjectMeta{Name: name},
		Status: corev1.NodeStatus{Conditions: []corev1.NodeCondition{{Type: corev1.NodeReady, Status: corev1.ConditionTrue}}}}
}

func nodeLease(name string, at time.Time) *coordinationv1.Lease {
	t := metav1.NewMicroTime(at)
	return &coordinationv1.Lease{ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: nodeLeaseNS}, Spec: coordinationv1.LeaseSpec{RenewTime: &t}}
}

func TestStartupAcceptsAConsistentMapping(t *testing.T) {
	client := fake.NewClientset(readyNode("n1"), readyNode("n2"), nodeLease("n1", time.Now()), nodeLease("n2", time.Now()))
	ok := statePower{"vm1": fence.Running, "vm2": fence.Running}
	if err := ValidateMachines(context.Background(), client, ok, map[string]string{"n1": "vm1", "n2": "vm2"}, time.Second, 10*time.Millisecond); err != nil {
		t.Fatal(err)
	}
}

func TestStartupComesUpDuringTheOutageItIsThereFor(t *testing.T) {
	// n1 lost power a few seconds ago: still Ready (the condition lags), machine off, no
	// heartbeat since. A supervisor restarting now must start, not refuse.
	client := fake.NewClientset(readyNode("n1"), readyNode("n2"), nodeLease("n1", time.Now().Add(-5*time.Second)), nodeLease("n2", time.Now()))
	power := statePower{"vm1": fence.Off, "vm2": fence.Running}
	if err := ValidateMachines(context.Background(), client, power, map[string]string{"n1": "vm1", "n2": "vm2"}, time.Second, 50*time.Millisecond); err != nil {
		t.Fatalf("refused to start during an outage: %v", err)
	}
}

func TestStartupRefusesAMappingWhoseNodeIsAliveOnAMachineThatIsOff(t *testing.T) {
	client := fake.NewClientset(readyNode("n1"), nodeLease("n1", time.Now()))
	power := statePower{"vm3": fence.Off} // n1 is mapped to the wrong machine
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go func() { // n1's kubelet keeps renewing
		for ctx.Err() == nil {
			_, _ = client.CoordinationV1().Leases(nodeLeaseNS).Update(ctx, nodeLease("n1", time.Now()), metav1.UpdateOptions{})
			time.Sleep(10 * time.Millisecond)
		}
	}()
	err := ValidateMachines(context.Background(), client, power, map[string]string{"n1": "vm3"}, time.Second, 100*time.Millisecond)
	var r Retryable
	if err == nil || errors.As(err, &r) || !strings.Contains(err.Error(), "mapping is wrong") {
		t.Fatalf("got %v", err)
	}
}

func TestStartupRetriesWhatItCannotSeeAndRefusesConfigurationErrors(t *testing.T) {
	client := fake.NewClientset(readyNode("n1"), nodeLease("n1", time.Now()))
	var r Retryable
	err := ValidateMachines(context.Background(), client, statePower{}, map[string]string{"n1": "vm9"}, time.Second, time.Millisecond)
	if !errors.As(err, &r) {
		t.Fatalf("an unreachable power controller must be retried, got %v", err)
	}
	err = ValidateMachines(context.Background(), client, statePower{"vm1": fence.Running}, map[string]string{"n1": "vm1", "n7": "vm2"}, time.Second, time.Millisecond)
	if err == nil || errors.As(err, &r) || !strings.Contains(err.Error(), "does not exist") {
		t.Fatalf("a node that does not exist is a configuration error: %v", err)
	}
	broken := fake.NewClientset()
	broken.PrependReactor("list", "nodes", func(k8stesting.Action) (bool, runtime.Object, error) {
		return true, nil, errors.New("context deadline exceeded")
	})
	if err := ValidateMachines(context.Background(), broken, statePower{}, map[string]string{"n1": "vm1"}, time.Second, time.Millisecond); !errors.As(err, &r) {
		t.Fatalf("an API timeout must be retried, got %v", err)
	}
}
