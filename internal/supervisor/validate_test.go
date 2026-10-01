package supervisor

import (
	"context"
	"strings"
	"testing"
	"time"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/kubernetes/fake"

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

func TestStartupRefusesAMappingThatContradictsWhatItSees(t *testing.T) {
	readyNode := func(name string) *corev1.Node {
		return &corev1.Node{ObjectMeta: metav1.ObjectMeta{Name: name},
			Status: corev1.NodeStatus{Conditions: []corev1.NodeCondition{{Type: corev1.NodeReady, Status: corev1.ConditionTrue}}}}
	}
	client := fake.NewClientset(readyNode("n1"), readyNode("n2"))
	ctx := context.Background()
	ok := statePower{"vm1": fence.Running, "vm2": fence.Running, "vm3": fence.Off}
	if err := ValidateMachines(ctx, client, ok, map[string]string{"n1": "vm1", "n2": "vm2"}, time.Second); err != nil {
		t.Fatal(err)
	}
	for name, c := range map[string]struct {
		machines map[string]string
		want     string
	}{
		"ready node on a machine that is off": {map[string]string{"n1": "vm3"}, "mapping is wrong"},
		"power controller cannot answer":      {map[string]string{"n1": "vm9"}, "cannot report"},
		"node that does not exist":            {map[string]string{"n1": "vm1", "n7": "vm2"}, "does not exist"},
	} {
		err := ValidateMachines(ctx, client, ok, c.machines, time.Second)
		if err == nil || !strings.Contains(err.Error(), c.want) {
			t.Fatalf("%s: got %v", name, err)
		}
	}
}
