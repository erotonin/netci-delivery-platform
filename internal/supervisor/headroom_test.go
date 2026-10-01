package supervisor

import (
	"context"
	"io"
	"log/slog"
	"strings"
	"testing"
	"time"

	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/testutil"
	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/client-go/kubernetes/fake"
)

func machine(name, cpu, mem string) corev1.Node {
	return corev1.Node{
		ObjectMeta: metav1.ObjectMeta{Name: name},
		Status: corev1.NodeStatus{
			Allocatable: corev1.ResourceList{corev1.ResourceCPU: resource.MustParse(cpu), corev1.ResourceMemory: resource.MustParse(mem)},
			Conditions:  []corev1.NodeCondition{{Type: corev1.NodeReady, Status: corev1.ConditionTrue}},
		},
	}
}

func workload(name, node, cpu, mem string, prio int32) corev1.Pod {
	return corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: "x", UID: types.UID(name)},
		Spec: corev1.PodSpec{NodeName: node, Priority: &prio, Containers: []corev1.Container{{Resources: corev1.ResourceRequirements{
			Requests: corev1.ResourceList{corev1.ResourceCPU: resource.MustParse(cpu), corev1.ResourceMemory: resource.MustParse(mem)}}}}},
		Status: corev1.PodStatus{Phase: corev1.PodRunning},
	}
}

// The lab's cell: a 1 CPU / 2Gi controller and a small cell-agent sidecar.
func controller(node string) corev1.Pod {
	p := workload("jenkins-0", node, "1", "2Gi", 900000)
	p.Namespace = "cell-b"
	p.OwnerReferences = []metav1.OwnerReference{{Kind: "StatefulSet", Name: "jenkins"}}
	always := corev1.ContainerRestartPolicyAlways
	p.Spec.InitContainers = []corev1.Container{{RestartPolicy: &always, Resources: corev1.ResourceRequirements{
		Requests: corev1.ResourceList{corev1.ResourceCPU: resource.MustParse("50m"), corev1.ResourceMemory: resource.MustParse("32Mi")}}}}
	return p
}

func room(t *testing.T, nodes []corev1.Node, pods []corev1.Pod, cell corev1.Pod) Room {
	t.Helper()
	r := Headroom(nodes, append(pods, cell), []corev1.Pod{cell})
	if len(r) != 1 || r[0].Cell != "cell-b/jenkins" {
		t.Fatalf("%+v", r)
	}
	return r[0]
}

func TestAMachineWithRoomCanTakeTheController(t *testing.T) {
	r := room(t, []corev1.Node{machine("lab-1", "4", "5Gi"), machine("lab-2", "4", "5Gi")}, nil, controller("lab-1"))
	if !r.Fits || r.Best != "lab-2" {
		t.Fatalf("%+v", r)
	}
}

// What chaos series 6 met: the survivor full of pods as important as the controller.
func TestNoMachineWithRoomIsReported(t *testing.T) {
	nodes := []corev1.Node{machine("lab-1", "4", "5Gi"), machine("lab-2", "4", "5Gi")}
	full := []corev1.Pod{workload("other-cell", "lab-2", "1", "2Gi", 900000), workload("platform", "lab-2", "500m", "1500Mi", 1000000)}
	r := room(t, nodes, full, controller("lab-1"))
	if r.Fits || !strings.Contains(r.Reason, "lab-2") || !strings.Contains(r.Reason, "lab-1 were lost") {
		t.Fatalf("%+v", r)
	}
}

func TestPodsOfLowerPriorityGiveWay(t *testing.T) {
	nodes := []corev1.Node{machine("lab-1", "4", "5Gi"), machine("lab-2", "4", "5Gi")}
	builds := []corev1.Pod{workload("sandbox-1", "lab-2", "1", "2Gi", -10), workload("agent", "lab-2", "1", "2Gi", 0)}
	if r := room(t, nodes, builds, controller("lab-1")); !r.Fits {
		t.Fatalf("builds counted as if they could not be evicted: %+v", r)
	}
}

func TestTheControllersOwnMachineAndUnusableOnesDoNotCount(t *testing.T) {
	cordoned := machine("lab-2", "4", "5Gi")
	cordoned.Spec.Unschedulable = true
	notReady := machine("lab-3", "4", "5Gi")
	notReady.Status.Conditions[0].Status = corev1.ConditionUnknown
	tainted := machine("lab-4", "4", "5Gi")
	tainted.Spec.Taints = []corev1.Taint{{Key: "dedicated", Value: "gpu", Effect: corev1.TaintEffectNoSchedule}}
	r := room(t, []corev1.Node{machine("lab-1", "4", "5Gi"), cordoned, notReady, tainted}, nil, controller("lab-1"))
	if r.Fits {
		t.Fatalf("counted its own machine, a cordoned one, one not Ready or one it cannot tolerate: %+v", r)
	}
	for _, want := range []string{"lab-2: cordoned", "lab-3: not Ready", "lab-4: taint dedicated"} {
		if !strings.Contains(r.Reason, want) {
			t.Errorf("reason %q lacks %q", r.Reason, want)
		}
	}
	tolerating := controller("lab-1")
	tolerating.Spec.Tolerations = []corev1.Toleration{{Key: "dedicated", Operator: corev1.TolerationOpExists}}
	if r := room(t, []corev1.Node{machine("lab-1", "4", "5Gi"), tainted}, nil, tolerating); !r.Fits {
		t.Fatalf("a tolerated taint excluded the machine: %+v", r)
	}
	softly := machine("lab-5", "4", "5Gi")
	softly.Spec.Taints = []corev1.Taint{{Key: "soft", Effect: corev1.TaintEffectPreferNoSchedule}}
	if r := room(t, []corev1.Node{machine("lab-1", "4", "5Gi"), softly}, nil, controller("lab-1")); !r.Fits {
		t.Fatalf("PreferNoSchedule excluded the machine: %+v", r)
	}
}

func TestTheSidecarAndFinishedPodsAreCountedAsTheSchedulerDoes(t *testing.T) {
	nodes := []corev1.Node{machine("lab-1", "4", "5Gi"), machine("lab-2", "4", "3Gi")}
	// 3Gi - 1Gi used = 2Gi left: enough for the controller alone, not with its 32Mi sidecar.
	used := []corev1.Pod{workload("platform", "lab-2", "100m", "1Gi", 1000000)}
	if r := room(t, nodes, used, controller("lab-1")); r.Fits {
		t.Fatalf("the sidecar's request was not counted: %+v", r)
	}
	done := workload("old-job", "lab-2", "100m", "1Gi", 1000000)
	done.Status.Phase = corev1.PodSucceeded
	if r := room(t, nodes, []corev1.Pod{done}, controller("lab-1")); !r.Fits {
		t.Fatalf("a finished pod was counted as using room: %+v", r)
	}
}

func TestAControllerWithNoMachineYetIsCheckedAgainstAll(t *testing.T) {
	r := room(t, []corev1.Node{machine("lab-1", "4", "5Gi")}, nil, controller(""))
	if !r.Fits {
		t.Fatalf("%+v", r)
	}
}

func TestCellsOnOneMachineAreCounted(t *testing.T) {
	a, b, c := controller("lab-3"), controller("lab-3"), controller("lab-1")
	if n := Sharing([]corev1.Pod{a, b, c}); n != 2 {
		t.Fatalf("%d cells sharing, want the two on lab-3", n)
	}
	if n := Sharing([]corev1.Pod{a, c}); n != 0 {
		t.Fatalf("%d", n)
	}
	gone := controller("lab-1")
	gone.Status.Phase = corev1.PodFailed
	if n := Sharing([]corev1.Pod{c, gone}); n != 0 {
		t.Fatalf("a finished pod counted: %d", n)
	}
}

// The check reads pods slimmed to what it uses; the verdict must not change.
func TestASlimPodGivesTheSameVerdict(t *testing.T) {
	nodes := []corev1.Node{machine("lab-1", "4", "5Gi"), machine("lab-2", "4", "3Gi")}
	used := []corev1.Pod{workload("platform", "lab-2", "100m", "1Gi", 1000000)}
	cell := controller("lab-1")
	full := Headroom(nodes, append(used, cell), []corev1.Pod{cell})
	var slimmed []corev1.Pod
	for i := range used {
		slimmed = append(slimmed, slim(&used[i]))
	}
	sc := slim(&cell)
	if got := Headroom(nodes, append(slimmed, sc), []corev1.Pod{sc}); got[0].Fits != full[0].Fits || got[0].Cell != full[0].Cell {
		t.Fatalf("slim %+v, full %+v", got, full)
	}
}

func TestTheWatchReportsACellWithoutHeadroom(t *testing.T) {
	cell := controller("lab-1")
	full := workload("platform", "lab-2", "500m", "3Gi", 1000000)
	n1, n2 := machine("lab-1", "4", "5Gi"), machine("lab-2", "4", "3Gi")
	client := fake.NewClientset(&n1, &n2, &cell, &full,
		&appsv1.StatefulSet{ObjectMeta: metav1.ObjectMeta{Name: "jenkins", Namespace: "cell-b", Labels: map[string]string{CellLabel: "true"}}})
	events := &fakeEvents{}
	m := NewMetrics(prometheus.NewRegistry())
	w := &HeadroomWatch{Client: client, Interval: time.Hour, Leading: func() bool { return true }, Events: events, Metrics: m,
		Log: slog.New(slog.NewTextHandler(io.Discard, nil))}
	if err := w.check(context.Background()); err != nil {
		t.Fatal(err)
	}
	if v := testutil.ToFloat64(m.headroom.WithLabelValues("cell-b/jenkins")); v != 0 {
		t.Fatalf("headroom %v, want 0: lab-2 is full", v)
	}
	if !events.has("NoTakeoverHeadroom") {
		t.Fatal("the leader did not report it")
	}
	_ = w.check(context.Background())
	if n := events.count("NoTakeoverHeadroom"); n != 1 {
		t.Fatalf("reported %d times; once until it changes", n)
	}
}
