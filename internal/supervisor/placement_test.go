package supervisor

import (
	"testing"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

func onNode(name, node string, ready bool) corev1.Pod {
	status := corev1.ConditionFalse
	if ready {
		status = corev1.ConditionTrue
	}
	return corev1.Pod{ObjectMeta: metav1.ObjectMeta{Name: name}, Spec: corev1.PodSpec{NodeName: node},
		Status: corev1.PodStatus{Phase: corev1.PodRunning, Conditions: []corev1.PodCondition{{Type: corev1.PodReady, Status: status}}}}
}

func TestALeaderBesideACellHandsOverToAReplicaOnAFreeMachine(t *testing.T) {
	sups := []corev1.Pod{onNode("sup-a", "lab-3", true), onNode("sup-b", "lab-1", true)}
	cells := []corev1.Pod{onNode("cell-a", "lab-3", true), onNode("cell-b", "lab-2", true)}
	if to, ok := YieldTo("sup-a", sups, cells); !ok || to != "sup-b" {
		t.Fatalf("%q %v", to, ok)
	}
	if _, ok := YieldTo("sup-b", sups, cells); ok {
		t.Fatal("a leader on a machine without a cell handed over")
	}
}

func TestNothingIsHandedOverWhenItGainsNothing(t *testing.T) {
	cells := []corev1.Pod{onNode("cell-a", "lab-1", true), onNode("cell-b", "lab-2", true)}
	for name, sups := range map[string][]corev1.Pod{
		"the other replica is beside a cell too": {onNode("sup-a", "lab-1", true), onNode("sup-b", "lab-2", true)},
		"the other replica is not Ready":         {onNode("sup-a", "lab-1", true), onNode("sup-b", "lab-3", false)},
		"the other replica is on the same node":  {onNode("sup-a", "lab-1", true), onNode("sup-b", "lab-1", true)},
		"there is no other replica":              {onNode("sup-a", "lab-1", true)},
	} {
		if to, ok := YieldTo("sup-a", sups, cells); ok {
			t.Errorf("%s: handed over to %s", name, to)
		}
	}
	gone := onNode("cell-a", "lab-1", true)
	gone.Status.Phase = corev1.PodFailed
	if _, ok := YieldTo("sup-a", []corev1.Pod{onNode("sup-a", "lab-1", true), onNode("sup-b", "lab-3", true)}, []corev1.Pod{gone}); ok {
		t.Error("a finished cell pod counted as a cell on the machine")
	}
}

func machineReady(name string) corev1.Node {
	return corev1.Node{ObjectMeta: metav1.ObjectMeta{Name: name},
		Status: corev1.NodeStatus{Conditions: []corev1.NodeCondition{{Type: corev1.NodeReady, Status: corev1.ConditionTrue}}}}
}

// What chaos series 8 left: both replicas beside cells, lab-1 free.
func TestAStandbyBesideACellIsMovedWhenAMachineIsFree(t *testing.T) {
	sups := []corev1.Pod{onNode("sup-a", "lab-2", true), onNode("sup-b", "lab-3", true)}
	cells := []corev1.Pod{onNode("cell-a", "lab-3", true), onNode("cell-b", "lab-2", true)}
	nodes := []corev1.Node{machineReady("lab-1"), machineReady("lab-2"), machineReady("lab-3")}
	if p, ok := Rebalance("sup-a", sups, cells, nodes); !ok || p.Name != "sup-b" {
		t.Fatalf("%q %v", p.Name, ok)
	}
	cordoned := machineReady("lab-1")
	cordoned.Spec.Unschedulable = true
	for name, n := range map[string][]corev1.Node{
		"no free machine":       {machineReady("lab-2"), machineReady("lab-3")},
		"the free one cordoned": {cordoned, machineReady("lab-2"), machineReady("lab-3")},
	} {
		if p, ok := Rebalance("sup-a", sups, cells, n); ok {
			t.Errorf("%s: moved %s", name, p.Name)
		}
	}
	// A replica already on a free machine: hand over instead, move nothing.
	ok := []corev1.Pod{onNode("sup-a", "lab-2", true), onNode("sup-b", "lab-1", true)}
	if p, moved := Rebalance("sup-a", ok, cells, nodes); moved {
		t.Errorf("moved %s although a hand-over was possible", p.Name)
	}
	// The leader on a free machine moves nothing.
	if p, moved := Rebalance("sup-b", []corev1.Pod{onNode("sup-a", "lab-2", true), onNode("sup-b", "lab-1", true)}, cells, nodes); moved {
		t.Errorf("moved %s with the leader already clear of cells", p.Name)
	}
}
